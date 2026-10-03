"""Single execution path for applying approved Terraform deployments (Phase 4A).

Hard safety rules & invariants:
1. The ONLY module that may execute `terraform apply`.
2. Only runs when TERRAAGENT_DEPLOY_ENABLED == "true" and queue routing is "deploy_apply".
3. Only applies if status is APPROVED, approval is not expired (<24h), and approved_by is set.
4. Computes plan bundle SHA-256 and asserts exact match with deployment.plan_bundle_sha256.
5. Executes strictly the exact command: [binary, "apply", "-input=false", "-lock-timeout=5m", "-no-color", "tfplan"].
6. `check_apply_argv` strictly validates argv with no extra flags or modifications permitted.
"""

import asyncio
from datetime import datetime, timezone
import json

import logging
import os
from typing import Any, Dict, List, Optional

from deploy.artifacts import get_artifact_store
from deploy.plan_bundle import create_plan_bundle, extract_plan_bundle
from deploy.store import DeployStatus, get_deployment, transition
from deploy.sts import apply_session, plan_session
from models.orm import AwsDeployTarget, Deployment
from services.database import SessionLocal
from services.redis_client import redis_service
from tools.credential_scrubber import CredentialScrubber
from tools.sandbox_registry import create_sandbox, release_sandbox
from tools.terraform_runner import ALLOWED_BINARIES, TerraformRunner

logger = logging.getLogger("terraagent.deploy.apply")

TERRAAGENT_TF_APPLY_TIMEOUT = float(os.getenv("TERRAAGENT_TF_APPLY_TIMEOUT", "2400"))
# -parallelism=20: independent resources (RDS, CloudFront, the build pipeline) are created
# concurrently instead of Terraform's default 10 at a time.
APPLY_ARGV_COMMON = ["apply", "-input=false", "-lock-timeout=5m", "-parallelism=20", "-no-color"]
DESTROY_PLAN_ARGV = ["plan", "-destroy", "-out=tfplan.destroy", "-input=false", "-lock=false"]
FORBIDDEN_APPLY_FLAGS = frozenset({"auto-approve", "target", "replace", "var", "var-file", "destroy", "import"})


def check_destroy_plan_argv(cmd: List[str]) -> str:
    """Strict argv validation for plan_destroy subprocess execution (Phase 5.2).
    Argv must equal [binary, "plan", "-destroy", "-out=tfplan.destroy", "-input=false", "-lock=false"].
    """
    if not cmd:
        raise ValueError("Safety Violation: empty command.")
    binary = os.path.basename(cmd[0]).lower()
    if binary.endswith(".exe"):
        binary = binary[:-4]
    if binary not in ALLOWED_BINARIES:
        raise ValueError(f"Safety Violation: only terraform/tofu allowed, got '{cmd[0]}'.")

    expected = [cmd[0]] + DESTROY_PLAN_ARGV
    if len(cmd) != len(expected):
        raise ValueError(f"Safety Violation: invalid argument count for plan_destroy. Expected {expected}, got {cmd}")
    for actual, exp in zip(cmd, expected):
        if actual.lower() != exp.lower() and exp not in (cmd[0],):
            raise ValueError(f"Safety Violation: illegal plan_destroy argument '{actual}', expected '{exp}'")
    return cmd[1]


def check_apply_argv(cmd: List[str], plan_kind: str = "apply") -> str:
    """Strict argv validation for apply subprocess execution.

    Only allows:
    [binary, "apply", "-input=false", "-lock-timeout=5m", "-parallelism=20", "-no-color", "tfplan"]
    (or "tfplan.destroy" if plan_kind == "destroy").
    """
    if not cmd:
        raise ValueError("Safety Violation: empty command.")

    binary = os.path.basename(cmd[0]).lower()
    if binary.endswith(".exe"):
        binary = binary[:-4]
    if binary not in ALLOWED_BINARIES:
        raise ValueError(f"Safety Violation: only terraform/tofu allowed, got '{cmd[0]}'.")

    expected_plan_file = "tfplan.destroy" if plan_kind == "destroy" else "tfplan"
    expected = [cmd[0]] + APPLY_ARGV_COMMON + [expected_plan_file]

    if len(cmd) != len(expected):
        raise ValueError(f"Safety Violation: invalid argument count for apply. Expected {expected}, got {cmd}")

    for actual, exp in zip(cmd, expected):
        if actual.lower() != exp.lower() and exp not in (cmd[0],):
            raise ValueError(f"Safety Violation: illegal apply argument '{actual}', expected '{exp}'")

    # Prohibit dangerous flags explicitly
    for arg in cmd[1:]:
        token = arg.lower().lstrip("-").split("=", 1)[0]
        if token in FORBIDDEN_APPLY_FLAGS and token != "destroy":
            raise ValueError(f"Safety Violation: prohibited apply flag '{arg}'.")
        if token == "destroy" and arg != "tfplan.destroy":
            raise ValueError(f"Safety Violation: prohibited flag '{arg}'.")

    return cmd[1]


async def _publish_log(deployment_id: str, message: str) -> None:
    scrubbed = CredentialScrubber.scrub_text(message)
    await redis_service.publish_log(deployment_id, f"[APPLY] {scrubbed}", agent_name="deploy")


def _scoped_aws_env(creds: Dict[str, str]) -> Dict[str, str]:
    env = os.environ.copy()
    env.pop("TF_LOG", None)
    env.pop("TF_LOG_PATH", None)
    env.update(creds)
    return env


async def apply_approved(deployment_id: str, routing_key: Optional[str] = None) -> Dict[str, Any]:
    """Applies an approved deployment against AWS with scoped STS credentials."""
    # 1. Check feature flag
    if os.environ.get("TERRAAGENT_DEPLOY_ENABLED", "").lower() != "true":
        logger.error(f"[{deployment_id}] apply_approved refused: TERRAAGENT_DEPLOY_ENABLED is not 'true'")
        return {"success": False, "error": "Deployment execution is disabled (TERRAAGENT_DEPLOY_ENABLED!=true)"}

    # Verify routing key if provided
    if routing_key is not None and routing_key != "deploy_apply":
        logger.error(f"[{deployment_id}] apply_approved refused: wrong routing key '{routing_key}', expected 'deploy_apply'")
        return {"success": False, "error": f"Invalid worker queue routing '{routing_key}'"}

    # 2. Load deployment under lock / session
    session = SessionLocal()
    try:
        dep: Optional[Deployment] = session.query(Deployment).filter(Deployment.id == deployment_id).first()
        if not dep:
            return {"success": False, "error": f"Deployment '{deployment_id}' not found"}

        if dep.status == DeployStatus.APPLYING.value:
            logger.warning(f"[{deployment_id}] Task started while status is already APPLYING. Transitioning to NEEDS_RECONCILIATION.")
            session.close()
            transition(
                deployment_id,
                DeployStatus.NEEDS_RECONCILIATION,
                actor="system",
                reason="Apply was already in progress or redelivered after worker restart. Manual reconciliation required.",
                error="Apply interrupted or redelivered. Review Terraform state lock and re-plan with read-only role.",
            )
            return {"success": False, "error": "Deployment requires manual reconciliation"}

        if dep.status != DeployStatus.APPROVED.value:
            logger.error(f"[{deployment_id}] apply_approved refused: status is '{dep.status}', expected 'APPROVED'")
            return {"success": False, "error": f"Deployment status is '{dep.status}', expected 'APPROVED'"}

        if not dep.approved_by:
            logger.error(f"[{deployment_id}] apply_approved refused: no approved_by recorded")
            return {"success": False, "error": "Deployment has no recorded approver"}

        # Check approval expiry (24h)
        if dep.approved_at:
            try:
                approved_time = datetime.fromisoformat(dep.approved_at)
                now_utc = datetime.now(timezone.utc) if approved_time.tzinfo else datetime.utcnow()
                if (now_utc - approved_time).total_seconds() > 86400:
                    session.close()
                    transition(deployment_id, DeployStatus.EXPIRED, actor="system", reason="Approval expired past 24 hours")
                    return {"success": False, "error": "Approval expired"}
            except Exception as e:
                logger.warning(f"Failed to check approval expiry on {deployment_id}: {e}")


        target_id = dep.target_id
        if not target_id:
            return {"success": False, "error": "No deploy target assigned"}

        target_orm = session.query(AwsDeployTarget).filter(AwsDeployTarget.id == target_id).first()
        if not target_orm:
            return {"success": False, "error": f"Target '{target_id}' not found"}

        target_dict = {
            "id": target_orm.id,
            "region": target_orm.region,
            "apply_role_arn": target_orm.apply_role_arn,
            "external_id": target_orm.external_id,
            "state_bucket": target_orm.state_bucket,
        }
        plan_kind = getattr(dep, "plan_kind", "plan") or "plan"
        approver_email = dep.approved_by
        expected_plan_sha = dep.plan_bundle_sha256
        plan_artifact_id = dep.plan_artifact_id
    finally:
        session.close()

    # 3. Target Lease (one apply per target account at a time)
    lease_key = f"deploy:lease:{target_id}"
    lease_acquired = False
    if redis_service._redis_available:
        try:
            client = await redis_service.get_client()
            if client:
                lease_acquired = bool(await client.set(lease_key, deployment_id, nx=True, ex=int(TERRAAGENT_TF_APPLY_TIMEOUT + 120)))
            else:
                lease_acquired = True
        except Exception as e:
            logger.warning(f"Failed to check Redis lease: {e}")
            lease_acquired = True
    else:
        lease_acquired = True

    if not lease_acquired:
        logger.error(f"[{deployment_id}] Concurrent apply rejected: target '{target_id}' is currently leased by another deployment")
        return {"success": False, "error": f"Target '{target_id}' currently has an active apply in progress"}

    # 4. Transition to APPLYING or DESTROYING
    target_status = DeployStatus.DESTROYING if plan_kind == "destroy" else DeployStatus.APPLYING
    transition(deployment_id, target_status, actor="system", reason=f"{'Destroy apply' if plan_kind == 'destroy' else 'Apply'} started on dedicated deploy worker")
    await _publish_log(deployment_id, f"Starting Terraform {'destroy apply' if plan_kind == 'destroy' else 'apply'} with approved plan bundle...")

    sandbox = create_sandbox(prefix="terraagent_deploy_apply_")
    try:
        # Fetch & verify plan bundle
        store = get_artifact_store()
        if not plan_artifact_id:
            raise ValueError("No plan bundle artifact id found on deployment")

        bundle_bytes = store.read_bytes(plan_artifact_id)
        extracted = extract_plan_bundle(bundle_bytes, sandbox)
        if extracted.sha256 != expected_plan_sha:
            raise ValueError(f"Plan bundle SHA-256 mismatch! Expected {expected_plan_sha}, computed {extracted.sha256}")

        await _publish_log(deployment_id, f"Plan bundle fingerprint verified: {extracted.sha256}")

        # Assume apply role
        await _publish_log(deployment_id, f"Assuming deploy apply role for approver '{approver_email}'...")
        aws_env = apply_session(target_dict, deployment_id, approver_email=approver_email)
        scoped_env = _scoped_aws_env(aws_env)

        binary = "terraform"

        # Init backend
        state_key = f"terraagent/{target_id}/{deployment_id}.tfstate"
        init_cmd = [
            binary,
            "init",
            "-input=false",
            "-no-color",
            f"-backend-config=bucket={target_dict['state_bucket']}",
            f"-backend-config=key={state_key}",
            f"-backend-config=region={target_dict['region']}",
        ]
        await _publish_log(deployment_id, "Running terraform init...")
        init_code, init_out, init_err = await TerraformRunner.run_command(init_cmd, cwd=sandbox, env=scoped_env)
        if init_code != 0:
            raise RuntimeError(f"Terraform init failed: {init_err or init_out}")

        # Validate apply command
        expected_plan_file = "tfplan.destroy" if plan_kind == "destroy" else "tfplan"
        apply_cmd = [binary] + APPLY_ARGV_COMMON + [expected_plan_file]
        check_apply_argv(apply_cmd, plan_kind=plan_kind)

        await _publish_log(deployment_id, f"Executing terraform apply -lock-timeout=5m {expected_plan_file}...")

        proc = await asyncio.create_subprocess_exec(
            *apply_cmd,
            cwd=sandbox,
            env=scoped_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _stream_output(stream: Any) -> None:
            if not stream:
                return
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                if decoded:
                    await _publish_log(deployment_id, decoded)

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _stream_output(proc.stdout),
                    _stream_output(proc.stderr),
                ),
                timeout=TERRAAGENT_TF_APPLY_TIMEOUT,
            )
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            raise TimeoutError(f"Terraform apply timed out after {TERRAAGENT_TF_APPLY_TIMEOUT} seconds")

        if proc.returncode != 0:
            raise RuntimeError(f"Terraform apply failed with exit code {proc.returncode}")

        await _publish_log(deployment_id, "Terraform execution completed successfully!")

        applied_at = datetime.utcnow().isoformat()
        if plan_kind == "destroy":
            transition(
                deployment_id,
                DeployStatus.DESTROYED,
                actor="system",
                reason="Deployment destroyed successfully from AWS",
                applied_at=applied_at,
                completed_at=applied_at,
                outputs={},
            )
            await _publish_log(deployment_id, "Teardown finished: DESTROYED.")
            outputs = {}
        else:
            # Read outputs via show -json
            show_cmd = [binary, "show", "-json"]
            show_code, show_out, show_err = await TerraformRunner.run_command(show_cmd, cwd=sandbox, env=scoped_env)
            outputs: Dict[str, Any] = {}
            if show_code == 0 and show_out:
                try:
                    state_data = json.loads(show_out)
                    raw_outputs = state_data.get("values", {}).get("outputs", {})
                    for k, v in raw_outputs.items():
                        outputs[k] = v.get("value")
                except Exception as e:
                    logger.warning(f"[{deployment_id}] Failed to parse outputs JSON: {e}")

            transition(
                deployment_id,
                DeployStatus.DEPLOYED,
                actor="system",
                reason="Deployment applied successfully to AWS",
                applied_at=applied_at,
                completed_at=applied_at,
                outputs=outputs,
            )
            await _publish_log(deployment_id, f"Deployment finished: DEPLOYED. Outputs: {json.dumps(outputs)}")

        # Delete plan bundle artifact
        if plan_artifact_id:
            try:
                store.delete(plan_artifact_id)
            except Exception:
                pass

        return {"success": True, "outputs": outputs}

    except Exception as e:
        logger.exception(f"[{deployment_id}] Apply failed: {e}")
        scrubbed_err = CredentialScrubber.scrub_text(str(e))
        await _publish_log(deployment_id, f"Apply error: {scrubbed_err}")
        transition(
            deployment_id,
            DeployStatus.FAILED_PARTIAL,
            actor="system",
            reason=f"Apply failed: {scrubbed_err}",
            error=scrubbed_err,
        )
        return {"success": False, "error": scrubbed_err}

    finally:
        release_sandbox(sandbox)
        if redis_service._redis_available:
            try:
                client = await redis_service.get_client()
                if client:
                    await client.delete(lease_key)
            except Exception:
                pass


async def plan_destroy(deployment_id: str, actor: str = "operator") -> Dict[str, Any]:
    """Phase 5.2: Narrowly validated destroy plan generation.
    Argv is strictly verified to match [binary, "plan", "-destroy", "-out=tfplan.destroy", "-input=false", "-lock=false"].
    """
    dep = get_deployment(deployment_id)
    if not dep:
        return {"success": False, "error": f"Deployment '{deployment_id}' not found"}

    allowed_from = frozenset({
        DeployStatus.DEPLOYED,
        DeployStatus.FAILED_PARTIAL,
        DeployStatus.NEEDS_RECONCILIATION,
    })
    if DeployStatus(dep["status"]) not in allowed_from:
        return {
            "success": False,
            "error": f"Cannot plan destroy from status '{dep['status']}'; must be in DEPLOYED, FAILED_PARTIAL, or NEEDS_RECONCILIATION."
        }

    target_id = dep.get("target_id")
    if not target_id:
        return {"success": False, "error": "No deploy target assigned to deployment"}

    session = SessionLocal()
    try:
        target_orm = session.query(AwsDeployTarget).filter(AwsDeployTarget.id == target_id).first()
        if not target_orm:
            return {"success": False, "error": f"Target '{target_id}' not found"}
        target_dict = {
            "id": target_orm.id,
            "region": target_orm.region,
            "plan_role_arn": target_orm.plan_role_arn,
            "apply_role_arn": target_orm.apply_role_arn,
            "external_id": target_orm.external_id,
            "state_bucket": target_orm.state_bucket,
        }
    finally:
        session.close()

    # 1. Transition to DESTROY_PLANNING
    transition(
        deployment_id,
        DeployStatus.DESTROY_PLANNING,
        actor=actor,
        reason="Destroy plan initiated by operator",
        allowed_from=allowed_from,
        plan_kind="destroy",
        is_destructive=True,
    )
    await _publish_log(deployment_id, "Initiating destroy plan generation with read-only STS plan role...")

    sandbox = create_sandbox(prefix="terraagent_deploy_destroy_plan_")
    try:
        plan_creds = await asyncio.to_thread(plan_session, target_dict, deployment_id)
        aws_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "TF_PLUGIN_CACHE_DIR": os.environ.get("TF_PLUGIN_CACHE_DIR", ""),
            "TF_IN_AUTOMATION": "1",
            "AWS_ACCESS_KEY_ID": plan_creds.get("access_key") or "",
            "AWS_SECRET_ACCESS_KEY": plan_creds.get("secret_key") or "",
            "AWS_DEFAULT_REGION": target_dict["region"],
        }
        if plan_creds.get("session_token"):
            aws_env["AWS_SESSION_TOKEN"] = plan_creds["session_token"]
        scoped_env = _scoped_aws_env(aws_env)

        # Write rendered files
        rendered = dep.get("rendered") or {}
        for fname, content in rendered.items():
            fpath = os.path.join(sandbox, fname)
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)

        binary = "terraform"

        # Init backend
        state_key = f"terraagent/{target_id}/{deployment_id}.tfstate"
        init_cmd = [
            binary,
            "init",
            "-input=false",
            "-no-color",
            f"-backend-config=bucket={target_dict['state_bucket']}",
            f"-backend-config=key={state_key}",
            f"-backend-config=region={target_dict['region']}",
        ]
        init_code, init_out, init_err = await TerraformRunner.run_command(init_cmd, cwd=sandbox, env=scoped_env)
        if init_code != 0:
            raise RuntimeError(f"Terraform init failed: {init_err or init_out}")

        # Execute destroy plan
        destroy_plan_cmd = [binary] + DESTROY_PLAN_ARGV
        check_destroy_plan_argv(destroy_plan_cmd)

        proc = await asyncio.create_subprocess_exec(
            *destroy_plan_cmd,
            cwd=sandbox,
            env=scoped_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace") or stdout.decode("utf-8", errors="replace")
            raise RuntimeError(f"Terraform plan -destroy failed: {err_msg}")

        # Show JSON
        show_cmd = [binary, "show", "-json", "tfplan.destroy"]
        show_code, show_out, show_err = await TerraformRunner.run_command(show_cmd, cwd=sandbox, env=scoped_env)
        if show_code != 0:
            raise RuntimeError(f"Terraform show -json failed: {show_err or show_out}")

        raw_plan_data = json.loads(show_out)
        scrubbed_plan_data = json.loads(CredentialScrubber.scrub_text(show_out))

        # Parse counts & changes
        resource_changes = raw_plan_data.get("resource_changes", []) or []
        num_destroys = 0
        changes_list = []
        for rc in resource_changes:
            actions = rc.get("change", {}).get("actions", [])
            addr = rc.get("address", "unknown")
            rtype = rc.get("type", "unknown")
            if "delete" in actions:
                num_destroys += 1
                changes_list.append({"address": addr, "action": "destroy", "type": rtype})

        summary = {
            "counts": {"create": 0, "update": 0, "replace": 0, "destroy": num_destroys, "no_op": 0},
            "changes": changes_list,
            "is_destructive": True,
        }

        # Package plan bundle
        bundle_bytes, bundle_sha256 = create_plan_bundle(sandbox)
        store = get_artifact_store()
        stored_artifact = store.put_bytes(bundle_bytes)

        transition(
            deployment_id,
            DeployStatus.AWAITING_APPROVAL,
            actor="system",
            reason=f"Destroy plan ready ({num_destroys} resources). Awaiting destructive approval.",
            plan=scrubbed_plan_data,
            plan_summary=summary,
            plan_bundle_sha256=bundle_sha256,
            plan_artifact_id=stored_artifact["artifact_id"],
            plan_kind="destroy",
            is_destructive=True,
        )
        await _publish_log(deployment_id, f"Destroy plan generated: {num_destroys} resources scheduled for destruction. Awaiting approval.")
        return {"success": True, "plan_bundle_sha256": bundle_sha256, "destroy_count": num_destroys}

    except Exception as e:
        logger.exception(f"[{deployment_id}] plan_destroy failed: {e}")
        scrubbed_err = CredentialScrubber.scrub_text(str(e))
        await _publish_log(deployment_id, f"Destroy plan error: {scrubbed_err}")
        transition(
            deployment_id,
            DeployStatus.FAILED,
            actor="system",
            reason=f"Destroy plan failed: {scrubbed_err}",
            error=scrubbed_err,
        )
        return {"success": False, "error": scrubbed_err}
    finally:
        release_sandbox(sandbox)
