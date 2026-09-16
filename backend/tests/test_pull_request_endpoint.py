"""API tests for POST /scan/{job_id}/pull-request. Requires a reachable
Redis, same as test_api.py/test_scan_approval_endpoints.py. Mocks
services.github_client.create_adoption_pr directly (the HTTP-call sequence
itself is covered by test_github_pr.py) so these focus on the endpoint's
own contract: job lookup, the COMPLETE-only gate, and that the token never
appears anywhere in the response or in what gets persisted."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from main import app
from services.redis_client import redis_service

FAKE_TOKEN = "ghp_supersecrettoken1234567890"


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
def client():
    with TestClient(app) as c:
        yield c


def _seed_complete_state(job_id: str):
    state = {
        "job_id": job_id,
        "status": "COMPLETE",
        "operation": "generate",
        "region": "us-east-1",
        "resources": [],
        "terraform_files": {"application.tf": 'resource "aws_s3_bucket" "b" {}'},
        "documentation": {"README.md": "# Bundle", "migration/import_plan.md": "..."},
        "adoption_plan": {"total_resource_count": 1, "risk_score": 10},
        "plan_equivalence_results": {"skipped": True, "reason": "not enabled"},
        "security_results": {"risk_score": 5, "findings": []},
        "cost_results": {"tool_skipped": True},
        "pending_approval": None,
        "approval_decision": None,
        "github_pr": None,
        "created_at": "2026-01-01T00:00:00",
    }
    asyncio.run(redis_service.set_job_state(job_id, state))


def test_pull_request_404_for_unknown_job(client):
    resp = client.post(
        "/api/scan/job-doesnotexist/pull-request",
        json={"github_token": FAKE_TOKEN, "repo": "my-org/my-repo"},
    )
    assert resp.status_code == 404


def test_pull_request_requires_complete_status(client, monkeypatch):
    job_id = "job-prnotcomplete"
    asyncio.run(redis_service.set_job_state(job_id, {"job_id": job_id, "status": "RUNNING"}))

    resp = client.post(
        f"/api/scan/{job_id}/pull-request",
        json={"github_token": FAKE_TOKEN, "repo": "my-org/my-repo"},
    )
    assert resp.status_code == 409


def test_pull_request_requires_terraform_files(client):
    job_id = "job-prnofiles001"
    asyncio.run(redis_service.set_job_state(job_id, {"job_id": job_id, "status": "COMPLETE", "terraform_files": {}}))

    resp = client.post(
        f"/api/scan/{job_id}/pull-request",
        json={"github_token": FAKE_TOKEN, "repo": "my-org/my-repo"},
    )
    assert resp.status_code == 409


def test_pull_request_success_persists_pr_info_never_the_token(client, monkeypatch):
    job_id = "job-prsuccess001"
    _seed_complete_state(job_id)

    async def fake_create_adoption_pr(**kwargs):
        assert kwargs["github_token"] == FAKE_TOKEN
        return {"pr_url": "https://github.com/my-org/my-repo/pull/3", "pr_number": 3, "branch": f"terraagent/adopt-{job_id}"}

    import services.github_client as github_client_module
    monkeypatch.setattr(github_client_module, "create_adoption_pr", fake_create_adoption_pr)

    resp = client.post(
        f"/api/scan/{job_id}/pull-request",
        json={"github_token": FAKE_TOKEN, "repo": "my-org/my-repo", "base_branch": "main"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pr_url"] == "https://github.com/my-org/my-repo/pull/3"
    assert body["pr_number"] == 3
    assert FAKE_TOKEN not in resp.text

    results = client.get(f"/api/scan/{job_id}/results").json()
    assert results["github_pr"]["pr_url"] == "https://github.com/my-org/my-repo/pull/3"
    assert FAKE_TOKEN not in client.get(f"/api/scan/{job_id}/results").text


def test_pull_request_github_error_returns_502_without_leaking_token(client, monkeypatch):
    job_id = "job-prerror00001"
    _seed_complete_state(job_id)

    from services.github_client import GitHubPullRequestError

    async def failing_create_adoption_pr(**kwargs):
        raise GitHubPullRequestError("Failed to create branch: 404 Not Found")

    import services.github_client as github_client_module
    monkeypatch.setattr(github_client_module, "create_adoption_pr", failing_create_adoption_pr)

    resp = client.post(
        f"/api/scan/{job_id}/pull-request",
        json={"github_token": FAKE_TOKEN, "repo": "my-org/my-repo"},
    )
    assert resp.status_code == 502
    assert FAKE_TOKEN not in resp.text


def test_pull_request_unknown_wave_404s(client):
    job_id = "job-prwavemiss01"
    _seed_complete_state(job_id)
    # adoption_plan has no "waves" key at all in _seed_complete_state - a
    # request for wave 1 must 404, not KeyError or silently ignore the wave.

    resp = client.post(
        f"/api/scan/{job_id}/pull-request",
        json={"github_token": FAKE_TOKEN, "repo": "my-org/my-repo", "wave": 1},
    )
    assert resp.status_code == 404


def test_pull_request_wave_scoped_success_persists_under_github_wave_prs(client, monkeypatch):
    job_id = "job-prwaveok0001"
    _seed_complete_state(job_id)
    state = asyncio.run(redis_service.get_job_state(job_id))
    state["adoption_plan"] = {
        "total_resource_count": 1,
        "risk_score": 10,
        "waves": [{"wave": 1, "resource_ids": ["bucket-1"], "risk_level": "low", "risk_signals": []}],
    }
    asyncio.run(redis_service.set_job_state(job_id, state))

    async def fake_create_adoption_pr(**kwargs):
        assert kwargs["wave"] == {"wave": 1, "resource_ids": ["bucket-1"], "risk_level": "low", "risk_signals": []}
        assert kwargs["github_token"] == FAKE_TOKEN
        return {
            "pr_url": "https://github.com/my-org/my-repo/pull/5", "pr_number": 5,
            "branch": f"terraagent/adopt-{job_id}-wave-1", "wave": 1,
        }

    import services.github_client as github_client_module
    monkeypatch.setattr(github_client_module, "create_adoption_pr", fake_create_adoption_pr)

    resp = client.post(
        f"/api/scan/{job_id}/pull-request",
        json={"github_token": FAKE_TOKEN, "repo": "my-org/my-repo", "wave": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["wave"] == 1
    assert FAKE_TOKEN not in resp.text

    results = client.get(f"/api/scan/{job_id}/results").json()
    assert results["github_wave_prs"]["1"]["pr_url"] == "https://github.com/my-org/my-repo/pull/5"
    # The whole-job github_pr field must stay untouched by a wave-scoped PR.
    assert results["github_pr"] is None
