"""Tests for Phase 3 Plan, Plan Bundle, Plan Policy, and Approval Gate."""

import asyncio
from datetime import datetime, timedelta
import io
import json
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from deploy.artifacts import LocalArtifactStore
from deploy.plan_bundle import create_plan_bundle, verify_plan_bundle
from deploy.plan_policy import evaluate_plan_policy
from deploy.store import DeployStatus, create_deployment, get_deployment, transition
from deploy.sts import plan_session
from models.deployment import ApprovalRequest, PlanRequest, RejectionRequest
from models.orm import AwsDeployTarget
import routers.deployments as api
from services.database import SessionLocal, init_db


@pytest.fixture(autouse=True)
def _trusted_sso_proxy(monkeypatch):
    # These tests model a deployment behind an authenticating SSO proxy, the
    # only setup in which identity/tenant headers are trusted (services/identity.py).
    monkeypatch.setenv("TERRAAGENT_TRUSTED_PROXY_AUTH", "true")


def _request(headers: list = None) -> Request:
    raw_headers = [(b"x-forwarded-email", b"test@example.com")] if headers is None else headers
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": raw_headers,
        "query_string": b"",
    })


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    init_db()
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    import deploy.pipeline as pipeline
    monkeypatch.setattr(api, "get_artifact_store", lambda: artifacts)
    monkeypatch.setattr(pipeline, "get_artifact_store", lambda: artifacts)


def test_plan_session_parameters():
    target = {
        "id": "target-123",
        "name": "Prod",
        "region": "us-east-1",
        "plan_role_arn": "arn:aws:iam::123456789012:role/TerraAgentDeployPlan",
        "apply_role_arn": "arn:aws:iam::123456789012:role/TerraAgentDeployApply",
        "state_bucket": "prod-state-bucket",
        "external_id": "test-ext-id-123",
    }

    with patch("deploy.sts.assume_role") as mock_assume:
        mock_assume.return_value = ("AKIA_PLAN", "SECRET_PLAN", "TOKEN_PLAN")
        creds = plan_session(target, "dep-abc123456789")

        assert creds["AWS_ACCESS_KEY_ID"] == "AKIA_PLAN"
        assert creds["AWS_SECRET_ACCESS_KEY"] == "SECRET_PLAN"
        assert creds["AWS_SESSION_TOKEN"] == "TOKEN_PLAN"

        mock_assume.assert_called_once()
        _, kwargs = mock_assume.call_args
        assert kwargs["role_arn"] == target["plan_role_arn"]
        assert kwargs["external_id"] == "test-ext-id-123"
        assert kwargs["source_identity"] == "terraagent-system"
        assert kwargs["duration_seconds"] == 900
        assert kwargs["tags"] == [{"Key": "deployment_id", "Value": "dep-abc123456789"}]


def test_plan_bundle_deterministic_hash(tmp_path):
    workdir = str(tmp_path / "workdir")
    os.makedirs(workdir, exist_ok=True)

    with open(os.path.join(workdir, "main.tf"), "w") as f:
        f.write('resource "aws_s3_bucket" "test" {}')
    with open(os.path.join(workdir, "tfplan"), "wb") as f:
        f.write(b"BINARY_PLAN_CONTENT")

    bundle1, hash1 = create_plan_bundle(workdir)
    bundle2, hash2 = create_plan_bundle(workdir)

    assert hash1 == hash2
    assert len(hash1) == 64
    assert verify_plan_bundle(bundle1, hash1) is True
    assert verify_plan_bundle(bundle1, "0" * 64) is False


def test_plan_policy_allowlist_and_destructiveness():
    # 1. Valid static_site plan
    valid_plan = {
        "resource_changes": [
            {
                "type": "aws_s3_bucket",
                "address": "aws_s3_bucket.site",
                "change": {"actions": ["create"], "after": {"bucket": "terraagent-dep1-site"}},
            },
            {
                "type": "aws_cloudfront_distribution",
                "address": "aws_cloudfront_distribution.cdn",
                "change": {"actions": ["create"], "after": {}},
            },
        ]
    }
    res = evaluate_plan_policy(valid_plan, "static_site", "dep1", monthly_cost=15.0)
    assert res["passed"] is True
    assert res["is_destructive"] is False
    assert len(res["violations"]) == 0

    # 2. Foreign resource type for static_site
    foreign_plan = {
        "resource_changes": [
            {
                "type": "aws_rds_cluster",
                "address": "aws_rds_cluster.db",
                "change": {"actions": ["create"], "after": {}},
            }
        ]
    }
    res_foreign = evaluate_plan_policy(foreign_plan, "static_site", "dep1")
    assert res_foreign["passed"] is False
    assert any("aws_rds_cluster" in v for v in res_foreign["violations"])

    # 3. Wildcard IAM policy action
    wildcard_iam_plan = {
        "resource_changes": [
            {
                "type": "aws_iam_role_policy",
                "address": "aws_iam_role_policy.exec",
                "change": {
                    "actions": ["create"],
                    "after": {
                        "policy": json.dumps({
                            "Version": "2012-10-17",
                            "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
                        })
                    },
                },
            }
        ]
    }
    res_iam = evaluate_plan_policy(wildcard_iam_plan, "lambda_http", "dep1")
    assert res_iam["passed"] is False
    assert any("forbidden wildcard Action" in v for v in res_iam["violations"])

    # 4. Destructive change detection (replace and delete)
    destructive_plan = {
        "resource_changes": [
            {
                "type": "aws_lambda_function",
                "address": "aws_lambda_function.api",
                "change": {"actions": ["delete", "create"], "after": {}},
            }
        ]
    }
    res_dest = evaluate_plan_policy(destructive_plan, "lambda_http", "dep1")
    assert res_dest["is_destructive"] is True
    assert len(res_dest["destructive_changes"]) == 1


def test_approval_gate_endpoints():
    dep_id = "dep-approval-test"
    create_deployment(dep_id, "zip", "app.zip", "us-east-1", "production", requested_by="alice@example.com")
    transition(dep_id, DeployStatus.ANALYZING)
    transition(dep_id, DeployStatus.ANALYZED, profile={"runtime": "static"}, decision={"eligible": ["static_site"], "recommended": "static_site"})
    transition(dep_id, DeployStatus.BUILDING)
    transition(dep_id, DeployStatus.VERIFYING)
    transition(dep_id, DeployStatus.VERIFIED, rendered={"main.tf": 'resource "aws_s3_bucket" "test" {}'})

    fake_sha256 = "a" * 64
    transition(dep_id, DeployStatus.PLANNING)
    transition(
        dep_id,
        DeployStatus.AWAITING_APPROVAL,
        plan={"resource_changes": []},
        plan_summary={"counts": {"create": 1, "update": 0, "replace": 0, "destroy": 0}, "changes": [], "is_destructive": False},
        plan_bundle_sha256=fake_sha256,
        is_destructive=False,
    )

    # 1. Unauthenticated approval (missing user header)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.approve_deployment(
            _request(headers=[]),
            dep_id,
            ApprovalRequest(plan_bundle_sha256=fake_sha256, confirm=True),
        ))
    assert exc.value.status_code == 401

    # 2. Approval with hash mismatch
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.approve_deployment(
            _request(headers=[(b"x-forwarded-email", b"bob@example.com")]),
            dep_id,
            ApprovalRequest(plan_bundle_sha256="b" * 64, confirm=True),
        ))
    assert exc.value.status_code == 400
    assert "mismatch" in exc.value.detail.lower()

    # 3. Successful approval
    res = asyncio.run(api.approve_deployment(
        _request(headers=[(b"x-forwarded-email", b"bob@example.com")]),
        dep_id,
        ApprovalRequest(plan_bundle_sha256=fake_sha256, confirm=True, reason="LGTM for production"),
    ))
    assert res.status == "APPROVED"

    dep = get_deployment(dep_id)
    assert dep["status"] == "APPROVED"
    assert dep["approved_by"] == "bob@example.com"
    assert dep["approval_reason"] == "LGTM for production"


def test_destructive_approval_requires_acknowledgment():
    dep_id = "dep-destructive-test"
    create_deployment(dep_id, "zip", "app.zip", "us-east-1", "production")
    transition(dep_id, DeployStatus.ANALYZING)
    transition(dep_id, DeployStatus.ANALYZED, profile={"runtime": "static"}, decision={"eligible": ["static_site"], "recommended": "static_site"})
    transition(dep_id, DeployStatus.BUILDING)
    transition(dep_id, DeployStatus.VERIFYING)
    transition(dep_id, DeployStatus.VERIFIED, rendered={"main.tf": 'resource "aws_s3_bucket" "test" {}'})

    fake_sha256 = "d" * 64
    transition(dep_id, DeployStatus.PLANNING)
    transition(
        dep_id,
        DeployStatus.AWAITING_APPROVAL,
        plan={"resource_changes": []},
        plan_summary={"counts": {"create": 0, "update": 0, "replace": 1, "destroy": 0}, "changes": [], "is_destructive": True},
        plan_bundle_sha256=fake_sha256,
        is_destructive=True,
    )

    # Approve without acknowledging destructive change
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.approve_deployment(
            _request(headers=[(b"x-forwarded-email", b"bob@example.com")]),
            dep_id,
            ApprovalRequest(plan_bundle_sha256=fake_sha256, confirm=True, acknowledge_destructive=False),
        ))
    assert exc.value.status_code == 400
    assert "destructive" in exc.value.detail.lower()

    # Approve with acknowledge_destructive = True
    res = asyncio.run(api.approve_deployment(
        _request(headers=[(b"x-forwarded-email", b"bob@example.com")]),
        dep_id,
        ApprovalRequest(plan_bundle_sha256=fake_sha256, confirm=True, acknowledge_destructive=True),
    ))
    assert res.status == "APPROVED"


def test_rejection_flow():
    dep_id = "dep-reject-test"
    create_deployment(dep_id, "zip", "app.zip", "us-east-1", "production")
    transition(dep_id, DeployStatus.ANALYZING)
    transition(dep_id, DeployStatus.ANALYZED, profile={"runtime": "static"}, decision={"eligible": ["static_site"], "recommended": "static_site"})
    transition(dep_id, DeployStatus.BUILDING)
    transition(dep_id, DeployStatus.VERIFYING)
    transition(dep_id, DeployStatus.VERIFIED)
    transition(dep_id, DeployStatus.PLANNING)
    transition(
        dep_id,
        DeployStatus.AWAITING_APPROVAL,
        plan={"resource_changes": []},
        plan_bundle_sha256="c" * 64,
    )

    res = asyncio.run(api.reject_deployment(
        _request(headers=[(b"x-forwarded-email", b"secops@example.com")]),
        dep_id,
        RejectionRequest(reason="Security concerns on IAM policy"),
    ))
    assert res.status == "REJECTED"

    dep = get_deployment(dep_id)
    assert dep["status"] == "REJECTED"
    assert dep["rejection_reason"] == "Security concerns on IAM policy"
