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


def test_hardening_pr_requires_the_adoption_pr_first(client):
    job_id = "job-hardenfirst"
    _seed_complete_state(job_id)
    state = asyncio.run(redis_service.get_job_state(job_id))
    state["hardening"] = {"files": {"data.tf": "x"}, "changes": [{}]}
    asyncio.run(redis_service.set_job_state(job_id, state))
    resp = client.post(f"/api/scan/{job_id}/pull-request",
                       json={"github_token": FAKE_TOKEN, "repo": "o/r", "kind": "hardening"})
    assert resp.status_code == 409 and "adoption PR first" in resp.json()["detail"]


def test_hardening_pr_requires_hardening_changes(client):
    job_id = "job-hardennone"
    _seed_complete_state(job_id)
    resp = client.post(f"/api/scan/{job_id}/pull-request",
                       json={"github_token": FAKE_TOKEN, "repo": "o/r", "kind": "hardening"})
    assert resp.status_code == 409 and "no validated hardening" in resp.json()["detail"]


def test_get_pull_request_details_success(client, monkeypatch):
    job_id = "job-getpr001"
    _seed_complete_state(job_id)
    state = asyncio.run(redis_service.get_job_state(job_id))
    state["github_pr"] = {
        "pr_url": "https://github.com/my-org/my-repo/pull/10",
        "pr_number": 10,
        "repo": "my-org/my-repo",
        "branch": "terraagent/adopt-job-getpr001",
        "status": "open",
    }
    asyncio.run(redis_service.set_job_state(job_id, state))

    async def fake_get_pr_details(**kwargs):
        return {
            "pr_number": 10,
            "title": "TerraAgent PR",
            "state": "open",
            "html_url": "https://github.com/my-org/my-repo/pull/10",
            "body": "PR Body",
            "head_branch": "terraagent/adopt-job-getpr001",
            "base_branch": "main",
            "head_sha": "abc1234",
            "mergeable": True,
            "mergeable_state": "clean",
            "merged": False,
            "additions": 25,
            "deletions": 0,
            "changed_files_count": 2,
            "changed_files": [{"filename": "terraform/main.tf", "status": "added", "additions": 25, "deletions": 0}],
            "reviews": [],
        }

    async def fake_get_pr_diff(**kwargs):
        return "+ resource aws_s3_bucket"

    async def fake_get_workflow_runs(**kwargs):
        return [{"id": 1, "name": "Terraform Deploy", "status": "completed", "conclusion": "success"}]

    import services.github_client as github_client_module
    monkeypatch.setattr(github_client_module, "get_pr_details", fake_get_pr_details)
    monkeypatch.setattr(github_client_module, "get_pr_diff", fake_get_pr_diff)
    monkeypatch.setattr(github_client_module, "get_workflow_runs", fake_get_workflow_runs)

    resp = client.get(
        f"/api/scan/{job_id}/pull-request",
        headers={"X-GitHub-Token": FAKE_TOKEN},
    )
    assert FAKE_TOKEN not in resp.text
    assert resp.status_code == 200
    data = resp.json()
    assert data["pr_number"] == 10
    assert data["state"] == "open"
    assert data["diff"] == "+ resource aws_s3_bucket"
    assert len(data["changed_files"]) == 1
    assert data["changed_files"][0]["filename"] == "terraform/main.tf"
    assert len(data["workflow_runs"]) == 1


def test_there_is_no_in_app_approval(client):
    # TerraAgent opens the PR, so approving it from here would be self-review.
    resp = client.post("/api/scan/job-x/pull-request/approve", json={})
    assert resp.status_code in (404, 405)


def _seed_with_prs(job_id, adoption_merged=False, hardening=False):
    _seed_complete_state(job_id)
    state = asyncio.run(redis_service.get_job_state(job_id))
    state["github_pr"] = {
        "pr_url": "https://github.com/my-org/my-repo/pull/10", "pr_number": 10, "repo": "my-org/my-repo",
        "branch": f"terraagent/adopt-{job_id}", "base_branch": "main", "merged": adoption_merged,
    }
    if hardening:
        state["github_hardening_pr"] = {
            "pr_url": "https://github.com/my-org/my-repo/pull/11", "pr_number": 11, "repo": "my-org/my-repo",
            "branch": f"terraagent/harden-{job_id}", "base_branch": f"terraagent/adopt-{job_id}",
        }
    asyncio.run(redis_service.set_job_state(job_id, state))


def _live(**overrides):
    details = {"state": "open", "merged": False, "mergeable": True, "mergeable_state": "clean",
               "base_branch": "main", "reviews": [{"user": "alice", "state": "APPROVED"}]}
    details.update(overrides)
    return details


def _patch_github(monkeypatch, details, merged_calls):
    import services.github_client as github_client_module

    async def fake_details(**kwargs):
        return details

    async def fake_merge(**kwargs):
        merged_calls.append(kwargs)
        return {"merged": True, "sha": "merged-sha", "message": "ok"}

    async def fake_runs(**kwargs):
        return [{"id": 99, "name": "Terraform CI/CD", "status": "queued", "conclusion": None}]

    monkeypatch.setattr(github_client_module, "get_pr_details", fake_details)
    monkeypatch.setattr(github_client_module, "merge_pr", fake_merge)
    monkeypatch.setattr(github_client_module, "get_workflow_runs", fake_runs)


def test_merge_pull_request_success_only_ever_targets_the_jobs_own_pr(client, monkeypatch):
    job_id = "job-merge001"
    _seed_with_prs(job_id)
    merged = []
    _patch_github(monkeypatch, _live(), merged)

    resp = client.post(f"/api/scan/{job_id}/pull-request/merge", json={
        "github_token": FAKE_TOKEN, "confirm": True, "merge_method": "squash",
        # a caller-supplied repo/PR is ignored - only the job's own PR is merged
        "repo": "someone-else/prod-infra", "pr_number": 1,
    })

    assert resp.status_code == 200 and resp.json()["merged"] is True
    assert merged[0]["repo"] == "my-org/my-repo" and merged[0]["pr_number"] == 10
    state = asyncio.run(redis_service.get_job_state(job_id))
    assert state["github_pr"]["merged"] is True
    assert FAKE_TOKEN not in str(state) and FAKE_TOKEN not in resp.text


@pytest.mark.parametrize("body_extra, live, expected", [
    ({"confirm": False}, {}, 422),
    ({}, {"mergeable": None, "mergeable_state": "unknown"}, 409),
    ({}, {"reviews": []}, 409),
    ({}, {"reviews": [{"user": "a", "state": "APPROVED"}, {"user": "b", "state": "CHANGES_REQUESTED"}]}, 409),
    ({}, {"merged": True}, 409),
])
def test_merge_is_refused_without_every_guard(client, monkeypatch, body_extra, live, expected):
    job_id = "job-mergeguard"
    _seed_with_prs(job_id)
    merged = []
    _patch_github(monkeypatch, _live(**live), merged)
    body = {"github_token": FAKE_TOKEN, "confirm": True, **body_extra}
    resp = client.post(f"/api/scan/{job_id}/pull-request/merge", json=body)
    assert resp.status_code == expected and merged == []


def test_merge_404_without_an_opened_pr(client):
    job_id = "job-mergenopr"
    _seed_complete_state(job_id)
    resp = client.post(f"/api/scan/{job_id}/pull-request/merge", json={"github_token": FAKE_TOKEN, "confirm": True})
    assert resp.status_code == 404


def test_hardening_merge_waits_for_the_adoption_merge_and_a_retarget(client, monkeypatch):
    job_id = "job-mergeharden"
    merged = []
    body = {"github_token": FAKE_TOKEN, "confirm": True, "kind": "hardening"}

    _seed_with_prs(job_id, adoption_merged=False, hardening=True)
    _patch_github(monkeypatch, _live(), merged)
    assert client.post(f"/api/scan/{job_id}/pull-request/merge", json=body).status_code == 409

    _seed_with_prs(job_id, adoption_merged=True, hardening=True)
    _patch_github(monkeypatch, _live(base_branch=f"terraagent/adopt-{job_id}"), merged)
    resp = client.post(f"/api/scan/{job_id}/pull-request/merge", json=body)
    assert resp.status_code == 409 and "Retarget" in resp.json()["detail"]

    _patch_github(monkeypatch, _live(base_branch="main"), merged)
    assert client.post(f"/api/scan/{job_id}/pull-request/merge", json=body).status_code == 200
    assert merged[-1]["pr_number"] == 11


def test_pr_status_without_token_returns_the_cached_record(client):
    job_id = "job-prcached"
    _seed_with_prs(job_id)
    data = client.get(f"/api/scan/{job_id}/pull-request").json()
    assert data["pr_number"] == 10 and data["repo"] == "my-org/my-repo" and data["diff"] is None
