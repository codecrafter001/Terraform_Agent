"""Tests for AWS Deploy Targets (Phase 3 task 3.2).

Covers registration, ExternalId generation, listing, deletion, and the verification endpoint.
"""

import asyncio
from unittest.mock import MagicMock, patch
import pytest
from starlette.requests import Request

from models.orm import AwsDeployTarget
from models.target import AwsDeployTargetCreate
from routers.aws_targets import (
    create_deploy_target,
    delete_deploy_target,
    get_deploy_target,
    list_deploy_targets,
    verify_deploy_target,
)
from services.database import SessionLocal, init_db


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""})


@pytest.fixture(autouse=True)
def cleanup_targets():
    init_db()
    session = SessionLocal()
    try:
        session.query(AwsDeployTarget).delete()
        session.commit()
    except Exception:
        pass
    finally:
        session.close()
    yield
    session = SessionLocal()
    try:
        session.query(AwsDeployTarget).delete()
        session.commit()
    except Exception:
        pass
    finally:
        session.close()


def test_create_deploy_target_generates_external_id():
    payload = AwsDeployTargetCreate(
        name="Production Account",
        account_id="123456789012",
        region="us-east-1",
        plan_role_arn="arn:aws:iam::123456789012:role/TerraAgentDeployPlan",
        apply_role_arn="arn:aws:iam::123456789012:role/TerraAgentDeployApply",
        permissions_boundary_arn="arn:aws:iam::123456789012:policy/TerraAgentWorkloadBoundary",
        state_bucket="my-terraagent-state-bucket",
    )
    res = asyncio.run(create_deploy_target(payload))

    assert res.id.startswith("target-")
    assert res.name == "Production Account"
    assert res.account_id == "123456789012"
    assert res.region == "us-east-1"
    assert res.external_id is not None
    assert len(res.external_id) >= 24
    assert res.verified_at is None


def test_list_and_delete_deploy_targets():
    payload = AwsDeployTargetCreate(
        name="Staging Account",
        account_id="111222333444",
        region="us-west-2",
        plan_role_arn="arn:aws:iam::111222333444:role/TerraAgentDeployPlan",
        apply_role_arn="arn:aws:iam::111222333444:role/TerraAgentDeployApply",
        state_bucket="staging-terraagent-bucket",
    )
    created = asyncio.run(create_deploy_target(payload))
    target_id = created.id

    targets = asyncio.run(list_deploy_targets())
    assert any(t.id == target_id for t in targets)

    got = asyncio.run(get_deploy_target(target_id))
    assert got.id == target_id

    asyncio.run(delete_deploy_target(target_id))

    targets_after = asyncio.run(list_deploy_targets())
    assert not any(t.id == target_id for t in targets_after)


@patch("routers.aws_targets.assume_role")
@patch("routers.aws_targets.boto3.Session")
def test_verify_deploy_target_success(mock_session_cls, mock_assume_role):
    # Mock STS AssumeRole return
    mock_assume_role.return_value = ("AKIA_TEST", "SECRET_TEST", "SESSION_TEST")

    # Mock boto3 Session & clients
    mock_session = MagicMock()
    mock_sts = MagicMock()
    mock_s3 = MagicMock()

    mock_sts.get_caller_identity.return_value = {
        "Account": "123456789012",
        "Arn": "arn:aws:sts::123456789012:assumed-role/TerraAgentDeployPlan/verify",
        "UserId": "AROA_TEST:verify",
    }
    mock_s3.list_objects_v2.return_value = {"KeyCount": 0}

    def client_side_effect(service_name):
        if service_name == "sts":
            return mock_sts
        if service_name == "s3":
            return mock_s3
        return MagicMock()

    mock_session.client.side_effect = client_side_effect
    mock_session_cls.return_value = mock_session

    # Create target
    payload = AwsDeployTargetCreate(
        name="Verified Account",
        account_id="123456789012",
        region="us-east-1",
        plan_role_arn="arn:aws:iam::123456789012:role/TerraAgentDeployPlan",
        apply_role_arn="arn:aws:iam::123456789012:role/TerraAgentDeployApply",
        state_bucket="verified-state-bucket",
    )
    created = asyncio.run(create_deploy_target(payload))
    target_id = created.id

    res = asyncio.run(verify_deploy_target(target_id))
    assert res.verified is True
    assert res.plan_role_ok is True
    assert res.apply_role_ok is True
    assert res.bucket_ok is True

    # Check that database verified_at was updated
    updated = asyncio.run(get_deploy_target(target_id))
    assert updated.verified_at is not None
