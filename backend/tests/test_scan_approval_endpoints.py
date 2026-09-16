"""API tests for POST /scan/{job_id}/approve and /reject - the human
approval gate that Real Terraform Plan + Human Approval Gate introduces.
Requires a reachable Redis, same as test_api.py, since these endpoints
read/write job state through it, and the resumed tail (cost_agent +
documentation_agent) persists its own result through it too."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from main import app
from services.redis_client import redis_service


def _redis_reachable() -> bool:
    try:
        asyncio.run(redis_service.get_client())
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_reachable(), reason="Redis is not reachable at REDIS_URL - required for API tests"
)


@pytest.fixture
def client(monkeypatch):
    # documentation_agent_node's LLM call and cost_agent_node's Infracost
    # subprocess are irrelevant to what these tests verify (the
    # approve/reject HTTP contract and state-machine transitions) - force
    # both onto their fast, deterministic fallback paths, same pattern as
    # test_documentation_agent.py's truncation test.
    import agents.documentation_agent as doc_module

    monkeypatch.delenv("INFRACOST_API_KEY", raising=False)

    async def _raise(*a, **k):
        raise RuntimeError("LLM unavailable in test")

    monkeypatch.setattr(doc_module.ollama_client, "generate", _raise)

    with TestClient(app) as c:
        yield c


def _finding():
    return {
        "tool": "terraform_plan", "rule_id": "plan-equivalence", "severity": "HIGH",
        "description": "terraform plan reports a `replace` action against `aws_db_instance.mydb`.",
        "resource": "aws_db_instance.mydb", "tier": "behavior_changing",
    }


def _seed_awaiting_approval_state(job_id: str, findings):
    state = {
        "job_id": job_id,
        "status": "AWAITING_APPROVAL",
        "operation": "generate",
        "region": "us-east-1",
        "resources": [],
        "terraform_files": {"application.tf": 'resource "aws_db_instance" "mydb" {}'},
        "dependency_graph": {},
        "classification_results": {"classifications": []},
        "adoption_plan": {},
        "validation_results": {"passed": True, "checks": []},
        "security_results": {"risk_score": 20, "findings": []},
        "cost_results": {},
        "plan_equivalence_results": {
            "passed": False,
            "blocking_actions": [{"address": "aws_db_instance.mydb", "action": "replace"}],
        },
        "pending_approval": {"reason": "plan_equivalence_requires_human_approval", "findings": findings},
        "repair_risk_tier": "behavior_changing",
        "approval_decision": None,
        "completed_agents": [
            "intent_router", "cloud_discovery", "graph_agent", "classification_agent",
            "adoption_planning_agent", "terraform_composer", "validation_agent", "plan_equivalence_agent",
        ],
        "current_agent": "awaiting_approval",
        "progress_percentage": 70,
        "agent_timings": {},
        "created_at": "2026-01-01T00:00:00",
        "webhook_url": None,
        "zip_password": None,
    }
    asyncio.run(redis_service.set_job_state(job_id, state))
    return state


def _wait_for_status(client, job_id, terminal_statuses, timeout=15):
    deadline = time.monotonic() + timeout
    last_body = None
    while time.monotonic() < deadline:
        resp = client.get(f"/api/scan/{job_id}/status")
        last_body = resp.json()
        if last_body.get("status") in terminal_statuses:
            return last_body
        time.sleep(0.2)
    raise AssertionError(f"Job {job_id} never reached {terminal_statuses}, last seen: {last_body}")


def test_approve_requires_awaiting_approval_status(client):
    job_id = "job-approve0001"
    asyncio.run(redis_service.set_job_state(job_id, {"job_id": job_id, "status": "RUNNING"}))

    resp = client.post(f"/api/scan/{job_id}/approve")
    assert resp.status_code == 409


def test_reject_requires_awaiting_approval_status(client):
    job_id = "job-reject00001"
    asyncio.run(redis_service.set_job_state(job_id, {"job_id": job_id, "status": "RUNNING"}))

    resp = client.post(f"/api/scan/{job_id}/reject")
    assert resp.status_code == 409


def test_approve_unknown_job_404s(client):
    resp = client.post("/api/scan/job-doesnotexist/approve")
    assert resp.status_code == 404


def test_approve_resumes_pipeline_to_complete(client):
    job_id = "job-approveok01"
    _seed_awaiting_approval_state(job_id, [_finding()])

    resp = client.post(f"/api/scan/{job_id}/approve", json={"reason": "Reviewed, safe to proceed"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "RUNNING"

    final = _wait_for_status(client, job_id, {"COMPLETE", "FAILED"})
    assert final["status"] == "COMPLETE"

    results = client.get(f"/api/scan/{job_id}/results").json()
    assert results["approval_decision"]["decision"] == "approved"
    assert results["approval_decision"]["reason"] == "Reviewed, safe to proceed"
    # The finding stays visible for audit even after approval - approving
    # means proceeding despite it, not erasing that it happened.
    assert results["pending_approval"]["findings"][0]["resource"] == "aws_db_instance.mydb"
    assert results["zip_available"] is True


def test_reject_halts_permanently_with_audit_trail(client):
    job_id = "job-rejectok01"
    _seed_awaiting_approval_state(job_id, [_finding()])

    resp = client.post(f"/api/scan/{job_id}/reject", json={"reason": "Not safe to adopt"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"

    final = _wait_for_status(client, job_id, {"REJECTED", "FAILED"})
    assert final["status"] == "REJECTED"

    results = client.get(f"/api/scan/{job_id}/results").json()
    assert results["approval_decision"]["decision"] == "rejected"
    assert results["approval_decision"]["reason"] == "Not safe to adopt"


def test_second_approve_after_already_resolved_is_conflict(client):
    job_id = "job-doubleapprv"
    _seed_awaiting_approval_state(job_id, [_finding()])

    first = client.post(f"/api/scan/{job_id}/approve")
    assert first.status_code == 200
    _wait_for_status(client, job_id, {"COMPLETE", "FAILED"})

    second = client.post(f"/api/scan/{job_id}/approve")
    assert second.status_code == 409
