"""API tests for POST /scan/{job_id}/approve and /reject - the human
approval gate. A job is paused for real (a stubbed graph run through
services/pipeline.py, stopping at the Delivery & Approval Agent's interrupt),
then decided through the API. Requires a reachable Redis, same as
test_api.py."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

import agents.graph as graph_mod
import routers.scan as scan_router
from main import app
from services.pipeline import run_pipeline
from services.redis_client import redis_service
from tests.test_graph_agent_loop import (  # noqa: F401
    DRIFT_FINDING, REVIEW_CLASSIFICATION, _stub, _validation_passing_after, calls,
)


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
def client(monkeypatch, calls):  # noqa: F811
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))
    monkeypatch.setattr(graph_mod, "drift_reconciliation_agent_node", _stub(
        "drift_reconciliation_agent", calls, {"pending_approval": {"reason": "drift", "findings": [DRIFT_FINDING]}}))
    monkeypatch.setattr(graph_mod, "classification_agent_node",
                        _stub("classification_agent", calls, {"classification_results": REVIEW_CLASSIFICATION}))

    # Resume in-process instead of through a Celery broker.
    async def dispatch(job_id, decision, aws_credentials):
        await scan_router._resume_inline(job_id, decision, aws_credentials)
    monkeypatch.setattr(scan_router, "_dispatch_resume", dispatch)

    with TestClient(app) as c:
        yield c


def _pause(client, job_id: str) -> dict:
    client.portal.call(run_pipeline, job_id, {"created_at": "2026-01-01T00:00:00"})
    status = client.get(f"/api/scan/{job_id}/status").json()
    assert status["status"] == "AWAITING_APPROVAL"
    return client.get(f"/api/scan/{job_id}/results").json()


def _wait_for_status(client, job_id, terminal_statuses, timeout=15):
    deadline = time.monotonic() + timeout
    last_body = None
    while time.monotonic() < deadline:
        last_body = client.get(f"/api/scan/{job_id}/status").json()
        if last_body.get("status") in terminal_statuses:
            return last_body
        time.sleep(0.1)
    raise AssertionError(f"Job {job_id} never reached {terminal_statuses}, last seen: {last_body}")


def test_approve_requires_awaiting_approval_status(client):
    job_id = "job-approve0001"
    asyncio.run(redis_service.set_job_state(job_id, {"job_id": job_id, "status": "RUNNING"}))
    assert client.post(f"/api/scan/{job_id}/approve").status_code == 409


def test_reject_requires_awaiting_approval_status(client):
    job_id = "job-reject00001"
    asyncio.run(redis_service.set_job_state(job_id, {"job_id": job_id, "status": "RUNNING"}))
    assert client.post(f"/api/scan/{job_id}/reject").status_code == 409


def test_approve_unknown_job_404s(client):
    assert client.post("/api/scan/job-doesnotexist/approve").status_code == 404


def test_paused_job_lists_findings_and_review_resources(client):
    results = _pause(client, "job-gatelist01")
    request = results["approval_request"]
    assert request["findings"] == [DRIFT_FINDING]
    assert [r["resource_id"] for r in request["review_resources"]] == ["role-1"]


def test_approve_must_decide_every_review_resource(client):
    job_id = "job-approvemiss"
    _pause(client, job_id)
    resp = client.post(f"/api/scan/{job_id}/approve", json={"reason": "ok"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["missing"] == ["role-1"]
    bad = client.post(f"/api/scan/{job_id}/approve", json={"resource_decisions": {"role-1": "delete"}})
    assert bad.status_code == 422


def test_approve_resumes_pipeline_to_complete(client, calls):  # noqa: F811
    job_id = "job-approveok01"
    _pause(client, job_id)

    resp = client.post(f"/api/scan/{job_id}/approve",
                       json={"reason": "Reviewed, safe to proceed", "resource_decisions": {"role-1": "exclude"}})
    assert resp.status_code == 200 and resp.json()["status"] == "RUNNING"

    assert _wait_for_status(client, job_id, {"COMPLETE", "FAILED"})["status"] == "COMPLETE"
    results = client.get(f"/api/scan/{job_id}/results").json()
    assert results["approval_decision"]["decision"] == "approved"
    assert results["approval_decision"]["reason"] == "Reviewed, safe to proceed"
    # Approving means proceeding despite the finding, not erasing it.
    assert results["pending_approval"]["findings"][0]["resource"] == "aws_vpc.main"
    assert results["approval_request"] is None
    assert calls.count("cloud_discovery") == 1  # resumed, not re-run


def test_reject_halts_permanently_with_audit_trail(client, calls):  # noqa: F811
    job_id = "job-rejectok01"
    _pause(client, job_id)

    resp = client.post(f"/api/scan/{job_id}/reject", json={"reason": "Not safe to adopt"})
    assert resp.status_code == 200 and resp.json()["status"] == "REJECTED"

    assert _wait_for_status(client, job_id, {"REJECTED", "FAILED"})["status"] == "REJECTED"
    results = client.get(f"/api/scan/{job_id}/results").json()
    assert results["approval_decision"]["decision"] == "rejected"
    assert results["approval_decision"]["reason"] == "Not safe to adopt"
    assert "documentation_agent" in calls and "cost_agent" not in calls


def test_second_approve_after_already_resolved_is_conflict(client):
    job_id = "job-doubleapprv"
    _pause(client, job_id)
    body = {"resource_decisions": {"role-1": "exclude"}}

    assert client.post(f"/api/scan/{job_id}/approve", json=body).status_code == 200
    _wait_for_status(client, job_id, {"COMPLETE", "FAILED"})
    assert client.post(f"/api/scan/{job_id}/approve", json=body).status_code == 409


def test_paused_job_without_checkpoint_cannot_be_resumed(client):
    job_id = "job-legacypause"
    asyncio.run(redis_service.set_job_state(job_id, {"job_id": job_id, "status": "AWAITING_APPROVAL"}))
    resp = client.post(f"/api/scan/{job_id}/reject")
    assert resp.status_code == 409 and "re-run the scan" in resp.json()["detail"]
