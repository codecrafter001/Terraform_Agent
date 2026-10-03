"""Tests for Phase 5.1: Zero-Downtime Application Rollback."""

import json
import uuid
import pytest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from starlette.requests import Request

import deploy.artifacts
from deploy.artifacts import LocalArtifactStore, get_artifact_store
from deploy.plan_policy import evaluate_plan_policy
from deploy.renderer import render, TFVARS_FILENAME
from deploy.rollback import prepare_rollback, _can_rollback
from deploy.store import (
    DeployStatus,
    create_deployment,
    get_deployment,
    record_artifact,
    transition,
    update_fields,
)
from models.deployment import RollbackRequest
import routers.deployments as api
from services.database import init_db

# Approvals expire after 24 h (deploy/apply_runner.py): approve "now", not on a fixed date.
from datetime import datetime, timezone

_APPROVED_AT = datetime.now(timezone.utc).isoformat()


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
    # Mock artifact store in temp directory
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    monkeypatch.setattr("deploy.artifacts.get_artifact_store", lambda: store)
    monkeypatch.setattr("deploy.rollback.get_artifact_store", lambda: store)
    yield


@pytest.fixture
def deployed_static_site():
    dep_id = f"dep-static-{uuid.uuid4().hex[:8]}"
    create_deployment(dep_id, "zip", "app.zip", "us-east-1", "production", requested_by="alice@example.com")
    transition(dep_id, DeployStatus.ANALYZING)
    transition(dep_id, DeployStatus.ANALYZED, target_type="static_site")
    transition(dep_id, DeployStatus.BUILDING)
    transition(dep_id, DeployStatus.VERIFYING)
    transition(dep_id, DeployStatus.VERIFIED)

    rendered_files = {
        "main.tf": 'resource "aws_cloudfront_distribution" "site" {}',
        TFVARS_FILENAME: json.dumps({
            "deployment_id": dep_id,
            "region": "us-east-1",
            "environment": "production",
            "release_id": "rel-v2",
            "site_files": {},
        }),
    }
    target_id = "target-123"

    transition(dep_id, DeployStatus.PLANNING, target_id=target_id, rendered=rendered_files)
    transition(dep_id, DeployStatus.AWAITING_APPROVAL, plan_bundle_sha256="hash123")
    transition(dep_id, DeployStatus.APPROVED, approved_by="lead@example.com", approved_at=_APPROVED_AT)
    transition(dep_id, DeployStatus.APPLYING)
    transition(dep_id, DeployStatus.DEPLOYED, applied_at="2026-10-02T10:05:00Z")

    # Record two build artifacts (v1 and v2)
    store = deploy.artifacts.get_artifact_store()
    art1 = store.put_bytes(b"build v1 content")
    record_artifact(dep_id, "bundle", art1, sensitive=True, retention_days=30)

    art2 = store.put_bytes(b"build v2 content")
    record_artifact(dep_id, "bundle", art2, sensitive=True, retention_days=30)

    return dep_id, art1["artifact_id"], art2["artifact_id"]


@pytest.fixture
def deployed_lambda_http():
    dep_id = f"dep-lambda-{uuid.uuid4().hex[:8]}"
    create_deployment(dep_id, "zip", "app.zip", "us-east-1", "production", requested_by="bob@example.com")
    transition(dep_id, DeployStatus.ANALYZING)
    transition(dep_id, DeployStatus.ANALYZED, target_type="lambda_http")
    transition(dep_id, DeployStatus.BUILDING)
    transition(dep_id, DeployStatus.VERIFYING)
    transition(dep_id, DeployStatus.VERIFIED)

    store = deploy.artifacts.get_artifact_store()
    art1 = store.put_bytes(b"lambda v1 zip bytes")
    record_artifact(dep_id, "bundle", art1, sensitive=True, retention_days=30)

    rendered_files = {
        "main.tf": 'resource "aws_lambda_alias" "live" {}',
        TFVARS_FILENAME: json.dumps({
            "deployment_id": dep_id,
            "region": "us-east-1",
            "environment": "production",
            "runtime": "python3.11",
            "handler": "app.handler",
            "package_file": "artifacts/v2.zip",
            "package_sha256_b64": "hashv2==",
        }),
    }
    target_id = "target-lambda"

    transition(dep_id, DeployStatus.PLANNING, target_id=target_id, rendered=rendered_files)
    transition(dep_id, DeployStatus.AWAITING_APPROVAL, plan_bundle_sha256="lambdahash123")
    transition(dep_id, DeployStatus.APPROVED, approved_by="lead@example.com", approved_at=_APPROVED_AT)
    transition(dep_id, DeployStatus.APPLYING)
    transition(dep_id, DeployStatus.DEPLOYED, applied_at="2026-10-02T10:05:00Z")

    return dep_id, art1["artifact_id"]


@pytest.mark.asyncio
async def test_prepare_rollback_static_site_success(deployed_static_site):
    dep_id, art1_id, art2_id = deployed_static_site

    # Prepare rollback to v1 release
    res = await prepare_rollback(
        dep_id,
        target_release_id="rel-v1",
        actor="lead-devops@example.com",
        reason="Rollback frontend regression",
    )

    assert res["status"] == "PLANNING"
    assert res["release_id"] == "rel-v1"

    dep = get_deployment(dep_id)
    assert dep["status"] == "PLANNING"
    rendered = dep["rendered"]
    tfvars_data = json.loads(rendered[TFVARS_FILENAME])
    assert tfvars_data["release_id"] == "rel-v1"


@pytest.mark.asyncio
async def test_prepare_rollback_lambda_success(deployed_lambda_http):
    dep_id, art1_id = deployed_lambda_http

    res = await prepare_rollback(
        dep_id,
        target_artifact_id=art1_id,
        actor="lead-devops@example.com",
    )

    assert res["status"] == "PLANNING"
    dep = get_deployment(dep_id)
    assert dep["status"] == "PLANNING"
    tfvars_data = json.loads(dep["rendered"][TFVARS_FILENAME])
    assert tfvars_data["package_file"] == f"artifacts/{art1_id}.zip"


def test_plan_policy_permits_lambda_alias_update():
    plan_json = {
        "resource_changes": [
            {
                "address": "aws_lambda_alias.live",
                "type": "aws_lambda_alias",
                "change": {"actions": ["update"], "after": {"name": "live", "function_version": "1"}},
            },
            {
                "address": "aws_lambda_function.function",
                "type": "aws_lambda_function",
                "change": {"actions": ["update"], "after": {"function_name": "terraagent-dep-123"}},
            },
        ]
    }

    eval_res = evaluate_plan_policy(plan_json, target_type="lambda_http", deployment_id="dep-1234567890ab")
    assert eval_res["passed"] is True
    assert len(eval_res["violations"]) == 0
    assert eval_res["is_destructive"] is False


@pytest.mark.asyncio
async def test_api_rollback_endpoint_and_artifacts(deployed_static_site):
    dep_id, art1_id, art2_id = deployed_static_site

    # Fetch artifacts list
    artifacts = await api.get_deployment_artifacts(dep_id)
    assert len(artifacts) >= 2
    assert artifacts[0].artifact_id in (art1_id, art2_id)

    # Post rollback request
    with patch("routers.deployments._dispatch") as mock_dispatch:
        body = RollbackRequest(target_release_id="rel-v1", reason="Revert bad CSS")
        resp = await api.rollback_deployment(_request(), dep_id, body)
        assert resp.status == "PLANNING"
        mock_dispatch.assert_called_once_with("plan", dep_id)


@pytest.mark.asyncio
async def test_api_rollback_rejects_pre_deploy_status():
    dep_id = f"dep-fresh-{uuid.uuid4().hex[:8]}"
    create_deployment(dep_id, "zip", "app.zip", "us-east-1", "production")

    with pytest.raises(HTTPException) as exc:
        await api.rollback_deployment(_request(), dep_id, RollbackRequest())
    assert exc.value.status_code == 409
