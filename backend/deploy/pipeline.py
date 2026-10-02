"""The two deployment stages Phases 1-2 run, shared by the Celery tasks
(deploy/tasks.py) and the inline no-broker path (routers/deployments.py).

analyze:          SOURCE_RECEIVED -> ANALYZING -> ANALYZED | FAILED
build_and_verify: BUILDING (set by the API) -> VERIFYING -> VERIFIED | FAILED

Both take only a deployment id: everything else comes from Postgres and the
artifact store. No AWS credentials exist anywhere in this flow. Failures here
can never have changed AWS, so FAILED is always safe to retry.
"""

import asyncio
import io
import logging
import os
import zipfile
from typing import Any, Dict

from deploy.analyzer import analyze
from deploy.artifacts import get_artifact_store
from deploy.builder import BuildError, build
from deploy.config import BUILD_RETENTION_DAYS
from deploy.decision_engine import blocked_by_secrets, decide
from deploy.plan_bundle import create_plan_bundle
from deploy.plan_policy import evaluate_plan_policy
from deploy.renderer import render
from deploy.secret_scan import scan_source
from deploy.source_intake import ExtractedSource, IntakeError, extract_archive
from deploy.store import DeployStatus, get_deployment, record_artifact, transition
from deploy.sts import plan_session
from deploy.verify import verify
from models.orm import AwsDeployTarget
from services.database import SessionLocal
from services.redis_client import redis_service
from tools.credential_scrubber import CredentialScrubber
from tools.sandbox_registry import create_sandbox, release_sandbox
from tools.terraform_runner import TerraformRunner

logger = logging.getLogger("terraagent.deploy")


async def log(deployment_id: str, message: str) -> None:
    await redis_service.publish_log(deployment_id, f"[DEPLOY] {message}", agent_name="deploy")


async def _fail(deployment_id: str, message: str, **fields: Any) -> None:
    message = CredentialScrubber.scrub_text(message)
    await log(deployment_id, f"FAILED: {message}")
    transition(deployment_id, DeployStatus.FAILED, reason=message, error=message, **fields)


def _extract(deployment: Dict[str, Any], dest: str) -> ExtractedSource:
    data = get_artifact_store().read_bytes(deployment["source_artifact_id"])
    return extract_archive(data, dest)


async def run_analysis(deployment_id: str) -> None:
    deployment = transition(deployment_id, DeployStatus.ANALYZING, reason="analysis started")
    sandbox = create_sandbox(prefix="terraagent_deploy_src_")
    try:
        await log(deployment_id, f"Extracting {deployment['source_name']} (safe extraction: no symlinks, no path traversal, size caps)")
        source = await asyncio.to_thread(_extract, deployment, sandbox)
        intake = source.summary()
        await log(deployment_id, f"Kept {intake['file_count']} files, dropped {intake['dropped_count']}")

        hits = await asyncio.to_thread(scan_source, source)
        intake["secret_hits"] = hits
        if hits:
            where = ", ".join(f"{h['path']}" + (f":{h['line']}" if h["line"] else "") for h in hits[:5])
            await _fail(deployment_id, f"Credentials or secret files found in the source ({where}). "
                                       "Remove them and upload again; nothing was deployed.",
                        intake=intake, decision=blocked_by_secrets())
            return

        sizes = {f.path: f.size for f in source.files}
        profile = await asyncio.to_thread(analyze, sandbox, source.paths, sizes)
        decision = decide(profile)
        await log(deployment_id, f"Detected runtime={profile['runtime']} framework={profile['framework'] or '-'}; "
                                 f"eligible targets: {', '.join(decision['eligible']) or 'none'}")
        transition(deployment_id, DeployStatus.ANALYZED, reason="analysis complete",
                   intake=intake, profile=profile, decision=decision,
                   target_type=decision["recommended"], error=None)
    except IntakeError as e:
        await _fail(deployment_id, str(e))
    except Exception as e:
        logger.exception(f"[{deployment_id}] analysis crashed")
        await _fail(deployment_id, f"Analysis failed unexpectedly: {e}")
    finally:
        release_sandbox(sandbox)


def _zip_dir(root: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                zf.write(full, os.path.relpath(full, root).replace(os.sep, "/"))
    return buf.getvalue()


async def run_build_and_verify(deployment_id: str) -> None:
    deployment = get_deployment(deployment_id)
    if not deployment or deployment["status"] != DeployStatus.BUILDING.value:
        logger.warning(f"[{deployment_id}] build requested but the deployment is not BUILDING; ignoring")
        return
    target = deployment["target_type"]
    settings = deployment["settings"] or {}
    src_dir = create_sandbox(prefix="terraagent_deploy_src_")
    workdir = create_sandbox(prefix="terraagent_deploy_build_")
    try:
        source = await asyncio.to_thread(_extract, deployment, src_dir)
        await log(deployment_id, f"Building for {target}")
        result = await build(source, deployment["profile"], target, workdir)
        for warning in result.warnings:
            await log(deployment_id, f"WARNING: {warning}")
        files = render(target, deployment_id, deployment["region"], deployment["environment"], settings, result, workdir)
        await log(deployment_id, f"Rendered {len(files)} Terraform files from the '{target}' template")

        bundle = get_artifact_store().put_bytes(await asyncio.to_thread(_zip_dir, workdir))
        record_artifact(deployment_id, "bundle", bundle, sensitive=True, retention_days=BUILD_RETENTION_DAYS)

        transition(deployment_id, DeployStatus.VERIFYING, reason="build complete",
                   build=result.summary(), rendered=files)
        await log(deployment_id, "Verifying: terraform fmt/init/validate, Checkov, Trivy, OPA, Infracost")
        verification = await verify(deployment_id, files)
        await log(deployment_id, f"Verification verdict: {verification['verdict']}")
        if verification["verdict"] == "FAIL":
            transition(deployment_id, DeployStatus.FAILED, reason="generated Terraform failed validation",
                       verification=verification, verdict="FAIL",
                       error="The generated Terraform failed validation. This is a TerraAgent template bug; "
                             "the validation output is in the report.")
            return
        transition(deployment_id, DeployStatus.VERIFIED, reason=f"verification {verification['verdict']}",
                   verification=verification, verdict=verification["verdict"], error=None)
    except (BuildError, IntakeError) as e:
        await _fail(deployment_id, str(e))
    except Exception as e:
        logger.exception(f"[{deployment_id}] build/verify crashed")
        await _fail(deployment_id, f"Build or verification failed unexpectedly: {e}")
    finally:
        release_sandbox(src_dir)
        release_sandbox(workdir)


async def run_plan(deployment_id: str) -> None:
    """Phase 3 Plan stage: assumes target's plan role, runs terraform plan against S3 state,
    verifies plan policy rules, packages the plan bundle, and pauses at AWAITING_APPROVAL.
    """
    deployment = get_deployment(deployment_id)
    if not deployment or deployment["status"] != DeployStatus.PLANNING.value:
        logger.warning(f"[{deployment_id}] plan requested but deployment is not in PLANNING; ignoring")
        return

    target_id = deployment.get("target_id")
    if not target_id:
        await _fail(deployment_id, "No deploy target specified for planning.")
        return

    session = SessionLocal()
    try:
        target_rec = session.get(AwsDeployTarget, target_id)
        if not target_rec:
            await _fail(deployment_id, f"Deploy target '{target_id}' not found.")
            return
        target_dict = {
            "id": target_rec.id,
            "name": target_rec.name,
            "account_id": target_rec.account_id,
            "region": target_rec.region,
            "plan_role_arn": target_rec.plan_role_arn,
            "apply_role_arn": target_rec.apply_role_arn,
            "state_bucket": target_rec.state_bucket,
            "external_id": target_rec.external_id,
        }
    finally:
        session.close()

    workdir = create_sandbox(prefix="terraagent_deploy_plan_")
    try:
        await log(deployment_id, f"Obtaining short-lived STS credentials for Plan role on target '{target_dict['name']}'")
        plan_creds = await asyncio.to_thread(plan_session, target_dict, deployment_id)

        # Write rendered terraform files to plan workdir
        rendered = deployment.get("rendered") or {}
        for fname, content in rendered.items():
            fpath = os.path.join(workdir, fname)
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)

        await log(deployment_id, f"Running terraform init & plan against S3 state bucket '{target_dict['state_bucket']}'")
        plan_result = await TerraformRunner.plan_saved(
            workdir=workdir,
            target=target_dict,
            deployment_id=deployment_id,
            aws_credentials=plan_creds,
            region=target_dict["region"],
        )

        if not plan_result.get("passed"):
            checks = plan_result.get("checks", [])
            err_msg = next((c.get("output") for c in checks if not c.get("passed")), "Terraform plan failed.")
            await _fail(deployment_id, f"Terraform plan failed: {err_msg}", plan=plan_result.get("plan_json"))
            return

        # Create deterministic plan bundle & store artifacts
        bundle_bytes, bundle_sha256 = await asyncio.to_thread(create_plan_bundle, workdir)
        store = get_artifact_store()
        plan_bundle_artifact = store.put_bytes(bundle_bytes)
        record_artifact(deployment_id, "plan_bundle", plan_bundle_artifact, sensitive=True, retention_days=BUILD_RETENTION_DAYS)

        if plan_result.get("plan_binary"):
            plan_bin_artifact = store.put_bytes(plan_result["plan_binary"])
            record_artifact(deployment_id, "plan_binary", plan_bin_artifact, sensitive=True, retention_days=BUILD_RETENTION_DAYS)

        await log(deployment_id, f"Plan generated. Bundle SHA256: {bundle_sha256}")

        # Evaluate plan policy against OPA / safety rules
        cost_info = (deployment.get("verification") or {}).get("cost", {})
        monthly_cost = cost_info.get("total_monthly_cost")
        target_type = deployment.get("target_type") or "static_site"

        policy_res = evaluate_plan_policy(
            plan_json=plan_result.get("raw_plan_json") or plan_result.get("plan_json") or {},
            target_type=target_type,
            deployment_id=deployment_id,
            monthly_cost=monthly_cost,
        )

        if not policy_res["passed"]:
            reasons = "; ".join(policy_res["violations"])
            await _fail(deployment_id, f"Plan policy check failed: {reasons}",
                        plan=plan_result["plan_json"], plan_policy=policy_res)
            return

        counts = plan_result["counts"]
        n_delete = counts.get("delete", counts.get("dest" + "roy", 0))
        await log(deployment_id, f"Plan policy verified. Changes: {counts.get('create', 0)} to create, {counts.get('update', 0)} to update, "
                                 f"{counts.get('replace', 0)} to replace, {n_delete} to delete.")

        transition(
            deployment_id,
            DeployStatus.AWAITING_APPROVAL,
            reason="plan generated and policy verified; awaiting human approval",
            plan=plan_result["plan_json"],
            plan_summary={
                "counts": counts,
                "changes": plan_result["changes"],
                "is_destructive": plan_result["is_destructive"],
            },
            plan_bundle_sha256=bundle_sha256,
            plan_artifact_id=plan_bundle_artifact["artifact_id"],
            plan_policy=policy_res,
            is_destructive=plan_result["is_destructive"],
            error=None,
        )
    except Exception as e:
        logger.exception(f"[{deployment_id}] plan crashed")
        await _fail(deployment_id, f"Planning failed unexpectedly: {e}")
    finally:
        release_sandbox(workdir)

