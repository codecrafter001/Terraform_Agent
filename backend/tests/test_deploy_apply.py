"""Tests for Phase 4A: Terraform Apply Runner & Execution Safety."""

import ast
import os
from datetime import datetime, timedelta
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from deploy.apply_runner import check_apply_argv, apply_approved
from deploy.store import DeployStatus, create_deployment, get_deployment, record_artifact, transition, update_fields
from deploy.artifacts import get_artifact_store
from models.orm import AwsDeployTarget
from services.database import SessionLocal, init_db
from tools.terraform_runner import check_argv, TerraformRunner


@pytest.fixture(autouse=True)
def setup_db():
    init_db()
    yield


# ---------------------------------------------------------------------------
# 1. check_apply_argv unit tests
# ---------------------------------------------------------------------------

def test_check_apply_argv_valid():
    cmd = ["terraform", "apply", "-input=false", "-lock-timeout=5m", "-parallelism=20", "-no-color", "tfplan"]
    assert check_apply_argv(cmd) == "apply"

    cmd_tofu = ["tofu", "apply", "-input=false", "-lock-timeout=5m", "-parallelism=20", "-no-color", "tfplan"]
    assert check_apply_argv(cmd_tofu) == "apply"


def test_check_apply_argv_destroy_plan_kind():
    cmd = ["terraform", "apply", "-input=false", "-lock-timeout=5m", "-parallelism=20", "-no-color", "tfplan.destroy"]
    assert check_apply_argv(cmd, plan_kind="destroy") == "apply"


def test_check_apply_argv_rejects_empty():
    with pytest.raises(ValueError, match="Safety Violation: empty command"):
        check_apply_argv([])


def test_check_apply_argv_rejects_invalid_binary():
    cmd = ["bash", "apply", "-input=false", "-lock-timeout=5m", "-parallelism=20", "-no-color", "tfplan"]
    with pytest.raises(ValueError, match="Safety Violation: only terraform/tofu allowed"):
        check_apply_argv(cmd)


@pytest.mark.parametrize("forbidden_flag", [
    "-auto-approve",
    "--auto-approve",
    "-target=aws_s3_bucket.site",
    "-replace=aws_lambda_function.fn",
    "-var=foo=bar",
    "-var-file=secret.tfvars",
    "-destroy",
    "destroy",
    "import",
])
def test_check_apply_argv_rejects_forbidden_flags(forbidden_flag):
    cmd = ["terraform", "apply", "-input=false", "-lock-timeout=5m", "-parallelism=20", "-no-color", "tfplan", forbidden_flag]
    with pytest.raises(ValueError, match="Safety Violation"):
        check_apply_argv(cmd)


def test_check_apply_argv_rejects_extra_arguments():
    cmd = ["terraform", "apply", "-input=false", "-lock-timeout=5m", "-parallelism=20", "-no-color", "tfplan", "-parallelism=10"]
    with pytest.raises(ValueError, match="Safety Violation"):
        check_apply_argv(cmd)


# ---------------------------------------------------------------------------
# 2. Guardrail & Allowlist Integrity Tests
# ---------------------------------------------------------------------------

def test_check_argv_allowlist_remains_unchanged_and_blocks_apply():
    """Verify check_argv in tools/terraform_runner.py is strictly unchanged."""
    with pytest.raises(ValueError, match="Safety Violation"):
        check_argv(["terraform", "apply"])

    with pytest.raises(ValueError, match="Safety Violation"):
        check_argv(["terraform", "plan", "-destroy"])

    with pytest.raises(ValueError, match="Safety Violation"):
        check_argv(["terraform", "import", "aws_s3_bucket.site", "my-bucket"])


def test_import_boundary_ast_scan():
    """Verify apply_runner is only imported by deploy.tasks and deploy.executor."""
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    allowed_importers = {
        "tasks.py",
        "executor.py",
        "sandbox_runner.py",
        "test_deploy_apply.py",
        "test_deploy_destroy.py",
        "test_deploy_sandbox_suite.py",
    }


    for root, _, files in os.walk(backend_dir):
        for f in files:
            if not f.endswith(".py"):
                continue
            if f in allowed_importers:
                continue
            path = os.path.join(root, f)
            with open(path, "r", encoding="utf-8", errors="ignore") as src:
                try:
                    tree = ast.parse(src.read(), filename=path)
                except Exception:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for name in node.names:
                            assert "apply_runner" not in name.name, f"Forbidden import of apply_runner in {path}"
                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            assert "apply_runner" not in node.module, f"Forbidden import from apply_runner in {path}"


# ---------------------------------------------------------------------------
# 3. apply_approved Refusal Paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_refuses_when_feature_flag_is_disabled(monkeypatch):
    monkeypatch.setenv("TERRAAGENT_DEPLOY_ENABLED", "false")
    res = await apply_approved("dep-test-disabled", routing_key="deploy_apply")
    assert res["success"] is False
    assert "disabled" in res["error"].lower()


@pytest.mark.asyncio
async def test_apply_refuses_when_routing_key_is_wrong(monkeypatch):
    monkeypatch.setenv("TERRAAGENT_DEPLOY_ENABLED", "true")
    res = await apply_approved("dep-test-wrong-queue", routing_key="deploy_plan")
    assert res["success"] is False
    assert "invalid worker queue routing" in res["error"].lower()


@pytest.mark.asyncio
async def test_apply_refuses_when_status_not_approved(monkeypatch):
    monkeypatch.setenv("TERRAAGENT_DEPLOY_ENABLED", "true")
    dep_id = f"dep-status-{datetime.utcnow().timestamp()}"
    create_deployment(dep_id, "zip", "app.zip", "us-east-1", "production")
    
    res = await apply_approved(dep_id, routing_key="deploy_apply")
    assert res["success"] is False
    assert "expected 'APPROVED'" in res["error"]


@pytest.mark.asyncio
async def test_apply_refuses_when_approver_missing(monkeypatch):
    monkeypatch.setenv("TERRAAGENT_DEPLOY_ENABLED", "true")
    dep_id = f"dep-no-appr-{datetime.utcnow().timestamp()}"
    create_deployment(dep_id, "zip", "app.zip", "us-east-1", "production")
    transition(dep_id, DeployStatus.ANALYZING)
    transition(dep_id, DeployStatus.ANALYZED)
    transition(dep_id, DeployStatus.BUILDING)
    transition(dep_id, DeployStatus.VERIFYING)
    transition(dep_id, DeployStatus.VERIFIED)
    transition(dep_id, DeployStatus.PLANNING)
    transition(dep_id, DeployStatus.AWAITING_APPROVAL)
    # Move to APPROVED without approved_by
    transition(dep_id, DeployStatus.APPROVED, approved_by=None)

    res = await apply_approved(dep_id, routing_key="deploy_apply")
    assert res["success"] is False
    assert "no recorded approver" in res["error"].lower()


@pytest.mark.asyncio
async def test_apply_refuses_when_approval_expired(monkeypatch):
    monkeypatch.setenv("TERRAAGENT_DEPLOY_ENABLED", "true")
    dep_id = f"dep-expired-{datetime.utcnow().timestamp()}"
    create_deployment(dep_id, "zip", "app.zip", "us-east-1", "production")
    transition(dep_id, DeployStatus.ANALYZING)
    transition(dep_id, DeployStatus.ANALYZED)
    transition(dep_id, DeployStatus.BUILDING)
    transition(dep_id, DeployStatus.VERIFYING)
    transition(dep_id, DeployStatus.VERIFIED)
    transition(dep_id, DeployStatus.PLANNING)
    transition(dep_id, DeployStatus.AWAITING_APPROVAL)

    old_time = (datetime.utcnow() - timedelta(hours=25)).isoformat()
    transition(dep_id, DeployStatus.APPROVED, approved_by="user@example.com", approved_at=old_time)

    res = await apply_approved(dep_id, routing_key="deploy_apply")
    assert res["success"] is False
    assert "expired" in res["error"].lower()
    dep = get_deployment(dep_id)
    assert dep["status"] == DeployStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_apply_transitions_to_needs_reconciliation_on_redelivery(monkeypatch):
    """If a task starts when status is already APPLYING, it must transition to NEEDS_RECONCILIATION."""
    monkeypatch.setenv("TERRAAGENT_DEPLOY_ENABLED", "true")
    dep_id = f"dep-reconcile-{datetime.utcnow().timestamp()}"
    create_deployment(dep_id, "zip", "app.zip", "us-east-1", "production")
    transition(dep_id, DeployStatus.ANALYZING)
    transition(dep_id, DeployStatus.ANALYZED)
    transition(dep_id, DeployStatus.BUILDING)
    transition(dep_id, DeployStatus.VERIFYING)
    transition(dep_id, DeployStatus.VERIFIED)
    transition(dep_id, DeployStatus.PLANNING)
    transition(dep_id, DeployStatus.AWAITING_APPROVAL)
    transition(dep_id, DeployStatus.APPROVED, approved_by="admin@example.com", approved_at=datetime.utcnow().isoformat())
    transition(dep_id, DeployStatus.APPLYING)

    res = await apply_approved(dep_id, routing_key="deploy_apply")
    assert res["success"] is False
    assert "manual reconciliation" in res["error"].lower()
    dep = get_deployment(dep_id)
    assert dep["status"] == DeployStatus.NEEDS_RECONCILIATION.value


# ---------------------------------------------------------------------------
# 4. End-to-End Mocked Apply Flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_approved_success_flow(monkeypatch):
    monkeypatch.setenv("TERRAAGENT_DEPLOY_ENABLED", "true")

    # Register target
    session = SessionLocal()
    target_id = f"target-{int(datetime.utcnow().timestamp())}"
    target = AwsDeployTarget(
        id=target_id,
        name="Sandbox Account",
        account_id="123456789012",
        region="us-east-1",
        plan_role_arn="arn:aws:iam::123456789012:role/TerraAgentDeployPlan",
        apply_role_arn="arn:aws:iam::123456789012:role/TerraAgentDeployApply",
        state_bucket="terraagent-state-bucket",
        external_id="ext-12345",
        created_at=datetime.utcnow().isoformat(),
        updated_at=datetime.utcnow().isoformat(),
    )
    session.add(target)
    session.commit()
    session.close()

    dep_id = f"dep-apply-ok-{int(datetime.utcnow().timestamp())}"
    create_deployment(dep_id, "zip", "site.zip", "us-east-1", "production")
    transition(dep_id, DeployStatus.ANALYZING)
    transition(dep_id, DeployStatus.ANALYZED)
    transition(dep_id, DeployStatus.BUILDING)
    transition(dep_id, DeployStatus.VERIFYING)
    transition(dep_id, DeployStatus.VERIFIED)
    transition(dep_id, DeployStatus.PLANNING)
    transition(dep_id, DeployStatus.AWAITING_APPROVAL)

    # Store fake plan bundle
    store = get_artifact_store()
    fake_bundle = b"fake-plan-bundle-content"
    import hashlib
    bundle_sha = hashlib.sha256(fake_bundle).hexdigest()
    stored = store.put_bytes(fake_bundle)

    transition(
        dep_id,
        DeployStatus.APPROVED,
        approved_by="approver@example.com",
        approved_at=datetime.utcnow().isoformat(),
        target_id=target_id,
        plan_bundle_sha256=bundle_sha,
        plan_artifact_id=stored["artifact_id"],
    )

    # Mock extract_plan_bundle, apply_session, runner, and subprocess
    with patch("deploy.apply_runner.extract_plan_bundle") as mock_extract, \
         patch("deploy.apply_runner.apply_session") as mock_session, \
         patch("deploy.apply_runner.TerraformRunner.run_command", new_callable=AsyncMock) as mock_run_cmd, \
         patch("asyncio.create_subprocess_exec") as mock_exec:

        mock_extract.return_value = MagicMock(sha256=bundle_sha)
        mock_session.return_value = {
            "AWS_ACCESS_KEY_ID": "ASIAFAKE",
            "AWS_SECRET_ACCESS_KEY": "fake-secret",
            "AWS_SESSION_TOKEN": "fake-token",
            "AWS_DEFAULT_REGION": "us-east-1",
        }

        mock_run_cmd.side_effect = [
            (0, "init success", ""),  # init
            (0, '{"values":{"outputs":{"website_url":{"value":"https://d123.cloudfront.net"}}}}', ""),  # show -json
        ]

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.stdout.readline = AsyncMock(side_effect=[b"Apply complete! Resources: 2 added, 0 changed, 0 destroyed.\n", b""])
        mock_proc.stderr.readline = AsyncMock(side_effect=[b""])
        mock_exec.return_value = mock_proc

        result = await apply_approved(dep_id, routing_key="deploy_apply")

        assert result["success"] is True
        assert result["outputs"]["website_url"] == "https://d123.cloudfront.net"

        dep = get_deployment(dep_id)
        assert dep["status"] == DeployStatus.DEPLOYED.value
        assert dep["applied_at"] is not None
        assert dep["outputs"]["website_url"] == "https://d123.cloudfront.net"


def test_apply_failure_message_carries_terraform_errors():
    from deploy.apply_runner import _apply_failure_message

    lines = ["aws_vpc.main: Creating...",
             "Error: creating IAM Role (x-exec): NoSuchEntity: Scope ARN: "
             "arn:aws:iam::1:policy/TerraAgentWorkloadBoundary does not exist or is not attachable."]
    msg = _apply_failure_message(1, lines)
    assert msg.startswith("Terraform apply failed with exit code 1:\nError: creating IAM Role")
    assert "TerraAgentWorkloadBoundary" in msg
    assert _apply_failure_message(1, []) == "Terraform apply failed with exit code 1 and wrote nothing to stderr."
