"""Tests for Phase 6 Item 6: Scheduled End-to-End Sandbox Test Suite.

Verifies all 5 automated scenarios against a sandbox environment:
1. Static Site: Intake -> Analyze -> Build -> Plan -> Approve -> Deploy.
2. Lambda: Deploy -> Redeploy with versioned live alias.
3. Reject: Rejection at human gate.
4. Expiry: Approval window expiry -> Apply safely refused.
5. Worker Kill: Interrupted apply -> Sweep reconciliation to NEEDS_RECONCILIATION.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from deploy.sandbox_runner import (
    _scenario_approval_expiry,
    _scenario_approval_reject,
    _scenario_lambda_deploy_and_redeploy,
    _scenario_static_site_deploy,
    _scenario_worker_kill_recovery,
    run_sandbox_suite,
)
from deploy.store import DeployStatus, get_deployment
from main import app
from models.orm import AwsDeployTarget
from services.database import SessionLocal, get_job_record, init_db


@pytest.fixture(autouse=True)
def _setup_db():
    init_db()


@pytest.fixture
def target_id():
    init_db()
    session = SessionLocal()

    import uuid
    tid = f"target-sandbox-test-{uuid.uuid4().hex[:8]}"

    target = AwsDeployTarget(
        id=tid,
        name="Sandbox Account Test",
        account_id="123456789012",
        region="us-east-1",
        plan_role_arn="arn:aws:iam::123456789012:role/TerraAgentDeployPlan",
        apply_role_arn="arn:aws:iam::123456789012:role/TerraAgentDeployApply",
        state_bucket="terraagent-sandbox-test",
        external_id="ext-test",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    session.add(target)
    session.commit()
    session.close()
    return tid


def test_scenario_static_site_deploy(target_id):
    log = []
    res = asyncio.run(_scenario_static_site_deploy(target_id, log))
    assert res["status"] == "PASS"
    assert "passed" in log[-1]
    dep = get_deployment(res["deployment_id"])
    assert dep["status"] == DeployStatus.DEPLOYED.value
    assert dep["outputs"]["website_url"].startswith("https://")


def test_scenario_lambda_deploy_and_redeploy(target_id):
    log = []
    res = asyncio.run(_scenario_lambda_deploy_and_redeploy(target_id, log))
    assert res["status"] == "PASS"
    dep = get_deployment(res["deployment_id"])
    assert dep["status"] == DeployStatus.DEPLOYED.value
    assert dep["outputs"]["function_url"].startswith("https://")


def test_scenario_approval_reject(target_id):
    log = []
    res = asyncio.run(_scenario_approval_reject(target_id, log))
    assert res["status"] == "PASS"
    dep = get_deployment(res["deployment_id"])
    assert dep["status"] == DeployStatus.REJECTED.value
    assert dep["rejection_reason"] == "Excessive permissions in plan policy"


def test_scenario_approval_expiry(target_id):
    log = []
    res = asyncio.run(_scenario_approval_expiry(target_id, log))
    assert res["status"] == "PASS"
    assert "refused" in log[-1]


def test_scenario_worker_kill_recovery(target_id):
    log = []
    res = asyncio.run(_scenario_worker_kill_recovery(target_id, log))
    assert res["status"] == "PASS"
    dep = get_deployment(res["deployment_id"])
    assert dep["status"] == DeployStatus.NEEDS_RECONCILIATION.value


def test_full_sandbox_suite_run_and_audit_record(target_id):
    result = asyncio.run(run_sandbox_suite(target_id))
    assert result["verdict"] == "PASS"
    assert len(result["scenarios"]) == 5
    assert all(s["status"] == "PASS" for s in result["scenarios"])

    # Check audit record
    run_id = result["run_id"]
    record = get_job_record(run_id)
    assert record is not None
    assert record.operation == "deploy_e2e_sandbox"
    assert record.status == "COMPLETE"
    assert record.validation_passed is True

    # Check runs summary
    runs = json.loads(record.runs_summary)
    assert runs["verdict"] == "PASS"
    assert len(runs["stages"]) >= 1



def test_api_trigger_sandbox_test(target_id):
    with TestClient(app) as client:
        resp = client.post(f"/api/deployments/sandbox-test?target_id={target_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] == "PASS"
        assert len(data["scenarios"]) == 5
