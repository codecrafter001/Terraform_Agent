"""Tests for Phase 5.2: Teardown / Destroy Safety Extension."""

import json
import uuid
import pytest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from starlette.requests import Request

import deploy.artifacts
from deploy.apply_runner import (
    check_apply_argv,
    check_destroy_plan_argv,
    plan_destroy,
    apply_approved,
)
from deploy.artifacts import LocalArtifactStore
from deploy.store import (
    DeployStatus,
    create_deployment,
    get_deployment,
    record_artifact,
    transition,
    update_fields,
)
from models.deployment import ApprovalRequest
from models.orm import AwsDeployTarget
import routers.deployments as api
from services.database import SessionLocal, init_db
from tools.terraform_runner import check_argv


@pytest.fixture(autouse=True)
def _trusted_sso_proxy(monkeypatch):
    # These tests model a deployment behind an authenticating SSO proxy, the
    # only setup in which identity/tenant headers are trusted (services/identity.py).
    monkeypatch.setenv("TERRAAGENT_TRUSTED_PROXY_AUTH", "true")


def _request() -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"x-forwarded-email", b"devops@example.com")],
        "query_string": b"",
    })


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    init_db()
    monkeypatch.setenv("TERRAAGENT_DEPLOY_ENABLED", "true")
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    monkeypatch.setattr("deploy.artifacts.get_artifact_store", lambda: store)
    monkeypatch.setattr("deploy.apply_runner.get_artifact_store", lambda: store)
    yield


@pytest.fixture
def mock_target():
    session = SessionLocal()
    target_id = f"target-destroy-{uuid.uuid4().hex[:8]}"
    now = "2026-10-02T10:00:00Z"
    t = AwsDeployTarget(
        id=target_id,
        name="Destroy Target",
        account_id="123456789012",
        region="us-east-1",
        plan_role_arn="arn:aws:iam::123456789012:role/terraagent-plan",
        apply_role_arn="arn:aws:iam::123456789012:role/terraagent-apply",
        state_bucket="terraagent-state-bucket",
        external_id="ext-id-12345",
        created_at=now,
        updated_at=now,
    )
    session.add(t)
    session.commit()
    session.close()
    return target_id


@pytest.fixture
def deployed_service(mock_target):
    dep_id = f"dep-destroy-{uuid.uuid4().hex[:8]}"
    create_deployment(dep_id, "zip", "service.zip", "us-east-1", "production", requested_by="alice@example.com")
    transition(dep_id, DeployStatus.ANALYZING)
    transition(dep_id, DeployStatus.ANALYZED, target_type="static_site")
    transition(dep_id, DeployStatus.BUILDING)
    transition(dep_id, DeployStatus.VERIFYING)
    transition(dep_id, DeployStatus.VERIFIED)

    rendered_files = {
        "main.tf": 'resource "aws_s3_bucket" "b" { bucket = "my-site" }',
        "terraform.tfvars.json": '{"region": "us-east-1"}',
    }
    transition(dep_id, DeployStatus.PLANNING, target_id=mock_target, rendered=rendered_files)
    transition(dep_id, DeployStatus.AWAITING_APPROVAL, plan_bundle_sha256="fakehash")
    transition(dep_id, DeployStatus.APPROVED, approved_by="admin@example.com", approved_at="2026-10-02T10:00:00Z")
    transition(dep_id, DeployStatus.APPLYING)
    transition(dep_id, DeployStatus.DEPLOYED, applied_at="2026-10-02T10:05:00Z")
    return dep_id


def test_migration_runner_blocks_destroy():
    """Safety Invariant: migration-mode check_argv strictly forbids -destroy and destroy."""
    with pytest.raises(ValueError, match="strictly prohibited"):
        check_argv(["terraform", "plan", "-destroy"])

    with pytest.raises(ValueError, match="strictly prohibited"):
        check_argv(["terraform", "destroy"])

    with pytest.raises(ValueError, match="strictly prohibited"):
        check_argv(["tofu", "plan", "-destroy", "-out=tfplan"])


def test_check_destroy_plan_argv_exact_validation():
    """apply_runner.check_destroy_plan_argv strictly validates argv."""
    valid = ["terraform", "plan", "-destroy", "-out=tfplan.destroy", "-input=false", "-lock=false"]
    assert check_destroy_plan_argv(valid) == "plan"

    valid_tofu = ["tofu.exe", "plan", "-destroy", "-out=tfplan.destroy", "-input=false", "-lock=false"]
    assert check_destroy_plan_argv(valid_tofu) == "plan"

    # Rejects extra flags
    with pytest.raises(ValueError, match="invalid argument count"):
        check_destroy_plan_argv(["terraform", "plan", "-destroy", "-out=tfplan.destroy", "-input=false", "-lock=false", "-auto-approve"])

    # Rejects missing flags
    with pytest.raises(ValueError, match="invalid argument count"):
        check_destroy_plan_argv(["terraform", "plan", "-destroy", "-out=tfplan.destroy"])

    # Rejects wrong binary
    with pytest.raises(ValueError, match="only terraform/tofu allowed"):
        check_destroy_plan_argv(["bash", "plan", "-destroy", "-out=tfplan.destroy", "-input=false", "-lock=false"])


def test_check_apply_argv_with_plan_kind():
    """check_apply_argv accepts tfplan.destroy ONLY when plan_kind == 'destroy'."""
    normal_cmd = ["terraform", "apply", "-input=false", "-lock-timeout=5m", "-parallelism=20", "-no-color", "tfplan"]
    destroy_cmd = ["terraform", "apply", "-input=false", "-lock-timeout=5m", "-parallelism=20", "-no-color", "tfplan.destroy"]

    # Normal apply allows tfplan
    assert check_apply_argv(normal_cmd, plan_kind="apply") == "apply"

    # Normal apply rejects tfplan.destroy
    with pytest.raises(ValueError, match="illegal apply argument"):
        check_apply_argv(destroy_cmd, plan_kind="apply")

    # Destroy apply allows tfplan.destroy
    assert check_apply_argv(destroy_cmd, plan_kind="destroy") == "apply"

    # Destroy apply rejects tfplan
    with pytest.raises(ValueError, match="illegal apply argument"):
        check_apply_argv(normal_cmd, plan_kind="destroy")


@pytest.mark.asyncio
async def test_plan_destroy_generates_destroy_plan(deployed_service):
    """plan_destroy runs narrowly validated plan -destroy and transitions to AWAITING_APPROVAL."""
    mock_plan_json = {
        "format_version": "1.2",
        "resource_changes": [
            {
                "address": "aws_s3_bucket.b",
                "type": "aws_s3_bucket",
                "change": {"actions": ["delete"]},
            }
        ],
    }

    with patch("deploy.apply_runner.plan_session", return_value={"access_key": "AKIA...", "secret_key": "sec..."}), \
         patch("deploy.apply_runner.TerraformRunner.run_command", new_callable=AsyncMock) as mock_run, \
         patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:

        mock_run.return_value = (0, json.dumps(mock_plan_json), "")
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"", b"")
        mock_exec.return_value = mock_proc

        res = await plan_destroy(deployed_service, actor="lead@example.com")
        assert res["success"] is True
        assert res["destroy_count"] == 1

        dep = get_deployment(deployed_service)
        assert dep["status"] == "AWAITING_APPROVAL"
        assert dep["plan_kind"] == "destroy"
        assert dep["is_destructive"] is True
        assert dep["plan_summary"]["counts"]["destroy"] == 1


@pytest.mark.asyncio
async def test_approval_requires_acknowledgment_for_destroy_plan(deployed_service):
    """Approving a destroy plan requires acknowledge_destructive: true."""
    update_fields(
        deployed_service,
        plan_kind="destroy",
        is_destructive=True,
        plan_bundle_sha256="destroybundlehash123",
    )
    transition(deployed_service, DeployStatus.DESTROY_PLANNING)
    transition(deployed_service, DeployStatus.AWAITING_APPROVAL)

    req = _request()

    # Attempt approval without acknowledge_destructive -> 400
    with pytest.raises(HTTPException) as exc:
        await api.approve_deployment(
            req,
            deployed_service,
            ApprovalRequest(
                plan_bundle_sha256="destroybundlehash123",
                confirm=True,
                acknowledge_destructive=False,
            ),
        )
    assert exc.value.status_code == 400
    assert "destructive changes" in exc.value.detail

    # Approve with acknowledge_destructive -> success
    res = await api.approve_deployment(
        req,
        deployed_service,
        ApprovalRequest(
            plan_bundle_sha256="destroybundlehash123",
            confirm=True,
            acknowledge_destructive=True,
            reason="Decommissioning stale environment",
        ),
    )
    assert res.status == "APPROVED"

    dep = get_deployment(deployed_service)
    assert dep["status"] == "APPROVED"


@pytest.mark.asyncio
async def test_apply_destroy_flow_transitions_to_destroyed(deployed_service, tmp_path):
    """apply_approved applies tfplan.destroy and transitions to DESTROYED."""
    # Set up approved destroy plan
    store = deploy.artifacts.get_artifact_store()
    art = store.put_bytes(b"PK\x03\x04fake_bundle_content")
    bundle_sha = art["sha256"]

    update_fields(
        deployed_service,
        plan_kind="destroy",
        is_destructive=True,
        plan_artifact_id=art["artifact_id"],
        plan_bundle_sha256=bundle_sha,
        approved_by="lead@example.com",
        approved_at="2026-10-02T12:00:00Z",
    )
    transition(deployed_service, DeployStatus.DESTROY_PLANNING)
    transition(deployed_service, DeployStatus.AWAITING_APPROVAL)
    transition(deployed_service, DeployStatus.APPROVED, approved_by="lead@example.com", approved_at="2026-10-02T12:00:00Z")

    with patch("deploy.apply_runner.extract_plan_bundle") as mock_extract, \
         patch("deploy.apply_runner.apply_session", return_value={"access_key": "AKIA...", "secret_key": "sec..."}), \
         patch("deploy.apply_runner.TerraformRunner.run_command", new_callable=AsyncMock) as mock_run, \
         patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:

        class ExtractedBundle:
            sha256 = bundle_sha

        mock_extract.return_value = ExtractedBundle()
        mock_run.return_value = (0, "", "")

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.stdout.readline = AsyncMock(side_effect=[b"Destroy complete! Resources: 1 destroyed.\n", b""])
        mock_proc.stderr.readline = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)
        mock_exec.return_value = mock_proc

        result = await apply_approved(deployed_service, routing_key="deploy_apply")
        assert result["success"] is True

        dep = get_deployment(deployed_service)
        assert dep["status"] == "DESTROYED"
        assert dep["completed_at"] is not None
