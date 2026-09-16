"""API tests using FastAPI's TestClient for all HTTP endpoints, including
the SSE log stream. Requires a reachable Redis (REDIS_URL) - the same
Redis the real stack uses - since routers read/write job state through it."""

import asyncio

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
    # Prevent tests from dispatching real Celery work (which would make real,
    # if harmlessly-failing, network calls to AWS with the fake test
    # credentials) - API tests only need the HTTP contract, not a real
    # pipeline run.
    import routers.scan as scan_router_module

    monkeypatch.setattr(scan_router_module.run_scan_task, "delay", lambda *a, **kw: None)
    with TestClient(app) as c:
        yield c


def test_health_check(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["service"] == "terraagent-api"


def test_start_scan_returns_job_id_and_never_echoes_credentials(client):
    payload = {
        "aws_access_key": "AKIAIOSFODNN7EXAMPLE",
        "aws_secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "region": "us-east-1",
        "operation": "generate",
        "resource_filters": ["VPC"],
    }
    resp = client.post("/api/scan", json=payload)
    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"].startswith("job-")
    assert body["status"] == "RUNNING"
    assert "AKIAIOSFODNN7EXAMPLE" not in resp.text
    assert "wJalrXUtnFEMI" not in resp.text


def test_start_scan_rejects_missing_credentials(client):
    resp = client.post("/api/scan", json={"region": "us-east-1"})
    assert resp.status_code == 422


def test_scan_status_404_for_unknown_job(client):
    resp = client.get("/api/scan/job-doesnotexist/status")
    assert resp.status_code == 404


def test_scan_results_404_for_unknown_job(client):
    resp = client.get("/api/scan/job-doesnotexist/results")
    assert resp.status_code == 404


def test_job_detail_404_for_unknown_job(client):
    resp = client.get("/api/jobs/job-doesnotexist")
    assert resp.status_code == 404


def test_list_jobs_pagination_params_are_validated(client):
    resp = client.get("/api/jobs", params={"limit": 0})
    assert resp.status_code == 422

    resp = client.get("/api/jobs", params={"limit": 5, "offset": 0})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_download_404_for_unknown_job(client):
    resp = client.get("/api/download/job-doesnotexist")
    assert resp.status_code == 404


def test_scan_status_reflects_started_job(client):
    payload = {
        "aws_access_key": "AKIAIOSFODNN7EXAMPLE",
        "aws_secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "region": "us-east-1",
        "operation": "generate",
        "resource_filters": ["VPC"],
    }
    started = client.post("/api/scan", json=payload).json()
    job_id = started["job_id"]

    resp = client.get(f"/api/scan/{job_id}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["status"] in ("PENDING", "RUNNING", "COMPLETE", "FAILED")


class _FakeJobRecord:
    """Stand-in for a services.database.get_job_record(...) return value -
    monkeypatched in rather than seeded through a real DB write, because
    conftest.py's DATABASE_URL=sqlite:///:memory: gives every OS thread its
    own private, mutually-invisible in-memory database (SQLAlchemy's
    SingletonThreadPool): a row inserted directly from the test's own thread
    is never visible to the request handler, which TestClient dispatches on
    its own internal thread. Patching get_job_record's return value sidesteps
    that entirely and tests the actual reconstruction logic in isolation."""

    def __init__(self, status, created_at="2026-01-01T00:00:00", error=None):
        self.status = status
        self.created_at = created_at
        self.error = error


def test_scan_status_db_fallback_reports_rejected_as_terminal_not_running(client, monkeypatch):
    # Regression guard for a real bug: this DB-fallback path predates
    # AWAITING_APPROVAL/REJECTED and originally only recognized COMPLETE/
    # FAILED as terminal - a REJECTED job (which genuinely ran the full
    # pipeline tail before its status was overridden, see
    # routers/scan.py::_resume_after_decision) fell through to the "still
    # running" branch and was reported as 50% RUNNING forever once Redis no
    # longer had its state.
    import services.database as db
    monkeypatch.setattr(db, "get_job_record", lambda job_id: _FakeJobRecord("REJECTED"))

    resp = client.get("/api/scan/job-fake-rejected-01/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "REJECTED"
    assert body["progress_percentage"] == 100
    assert body["current_agent"] == "documentation_agent"
    assert len(body["completed_agents"]) == 13


def test_scan_status_db_fallback_reports_awaiting_approval_as_paused_not_terminal(client, monkeypatch):
    # AWAITING_APPROVAL is genuinely paused (a decision could still resume
    # it), not terminal - and unlike COMPLETE/REJECTED, the DB record alone
    # can't say exactly which agents ran before it halted, so
    # completed_agents/current_agent must stay unset rather than guess.
    import services.database as db
    monkeypatch.setattr(db, "get_job_record", lambda job_id: _FakeJobRecord("AWAITING_APPROVAL"))

    resp = client.get("/api/scan/job-fake-awaiting-01/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "AWAITING_APPROVAL"
    assert body["progress_percentage"] == 50
    assert body["current_agent"] is None
    assert body["completed_agents"] == []
