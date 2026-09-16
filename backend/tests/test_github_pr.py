"""Unit tests for services/github_client.py - mocks httpx.AsyncClient (same
spirit as test_infracost_runner.py mocking asyncio.create_subprocess_exec)
so these run without a real GitHub token or repo.

Two real bugs were found by checking this module against GitHub's actual,
documented Contents API contract (not caught by the original version of
this test file, which mocked every PUT as an unconditional 201 regardless
of whether the target file already existed):

1. `PUT contents/{path}` requires the existing file's `sha` to overwrite a
   file that's already there - the new branch inherits everything already
   in the target repo, and every PR commits a README.md, so this fired on
   the very first commit against almost any real repo.
2. A failure partway through (a commit, or the PR-open call) left the
   already-created branch behind, half-populated - and since the branch
   name is deterministic per job_id, retrying failed immediately with
   "Reference already exists" on the very next attempt, forever.

test_existing_file_gets_its_sha_included_in_the_commit and
test_failed_commit_deletes_the_branch_before_raising below guard both.
"""

import base64
from unittest.mock import patch

import pytest

from services.github_client import GitHubPullRequestError, create_adoption_pr
from tools.naming import unique_clean_name

FAKE_TOKEN = "ghp_supersecrettoken1234567890"


class _FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text or str(json_data or {})

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Strictly ordered responses, popped one per call regardless of HTTP
    method - the code under test issues a fully deterministic call sequence
    for a given scenario, so an ordered queue is enough and keeps these
    tests readable."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def _next(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self._responses.pop(0)

    async def get(self, path, **kwargs):
        return await self._next("GET", path, **kwargs)

    async def post(self, path, **kwargs):
        return await self._next("POST", path, **kwargs)

    async def put(self, path, **kwargs):
        return await self._next("PUT", path, **kwargs)

    async def delete(self, path, **kwargs):
        return await self._next("DELETE", path, **kwargs)


def _base_kwargs(**overrides):
    kwargs = dict(
        github_token=FAKE_TOKEN,
        repo="my-org/my-repo",
        job_id="job-abc123def456",
        tf_files={"application.tf": 'resource "aws_s3_bucket" "b" {}'},
        docs={"README.md": "# Bundle"},
        adoption_plan={"total_resource_count": 1, "risk_score": 10},
        plan_equivalence_results={"skipped": True, "reason": "not enabled"},
        security_results={"risk_score": 5, "findings": []},
        cost_results={"tool_skipped": True},
        pending_approval=None,
        approval_decision=None,
    )
    kwargs.update(overrides)
    return kwargs


@pytest.mark.asyncio
async def test_happy_path_creates_branch_commits_and_opens_pr():
    responses = [
        _FakeResponse(200, {"object": {"sha": "base-sha-123"}}),  # GET base ref
        _FakeResponse(201, {}),  # POST refs (create branch)
        _FakeResponse(404, text="Not Found"),  # GET contents/terraform/application.tf - new file
        _FakeResponse(201, {}),  # PUT contents - application.tf
        _FakeResponse(404, text="Not Found"),  # GET contents/README.md - new file (this repo has none)
        _FakeResponse(201, {}),  # PUT contents - README.md
        _FakeResponse(201, {"html_url": "https://github.com/my-org/my-repo/pull/7", "number": 7}),  # POST pulls
    ]
    fake_client = _FakeAsyncClient(responses)
    with patch("httpx.AsyncClient", return_value=fake_client):
        result = await create_adoption_pr(**_base_kwargs())

    assert result["pr_url"] == "https://github.com/my-org/my-repo/pull/7"
    assert result["pr_number"] == 7
    assert result["branch"] == "terraagent/adopt-job-abc123def456"

    methods_and_paths = [(c[0], c[1]) for c in fake_client.calls]
    assert methods_and_paths[0] == ("GET", "/repos/my-org/my-repo/git/ref/heads/main")
    assert methods_and_paths[1] == ("POST", "/repos/my-org/my-repo/git/refs")
    assert ("PUT", "/repos/my-org/my-repo/contents/terraform/application.tf") in methods_and_paths
    assert ("PUT", "/repos/my-org/my-repo/contents/README.md") in methods_and_paths
    assert methods_and_paths[-1] == ("POST", "/repos/my-org/my-repo/pulls")


@pytest.mark.asyncio
async def test_existing_file_gets_its_sha_included_in_the_commit():
    # Regression guard for bug #1: the target repo already has a README.md
    # (true for almost every real repo) - the commit for it MUST include
    # that file's sha, or GitHub's real API returns 422.
    responses = [
        _FakeResponse(200, {"object": {"sha": "base-sha-123"}}),
        _FakeResponse(201, {}),
        _FakeResponse(404, text="Not Found"),  # application.tf is new
        _FakeResponse(201, {}),
        _FakeResponse(200, {"sha": "existing-readme-blob-sha"}),  # README.md already exists
        _FakeResponse(200, {}),  # PUT (update, not create)
        _FakeResponse(201, {"html_url": "https://github.com/my-org/my-repo/pull/8", "number": 8}),
    ]
    fake_client = _FakeAsyncClient(responses)
    with patch("httpx.AsyncClient", return_value=fake_client):
        await create_adoption_pr(**_base_kwargs())

    put_calls = [c for c in fake_client.calls if c[0] == "PUT"]
    readme_put = next(c for c in put_calls if c[1] == "/repos/my-org/my-repo/contents/README.md")
    assert readme_put[2]["json"]["sha"] == "existing-readme-blob-sha"

    tf_put = next(c for c in put_calls if c[1] == "/repos/my-org/my-repo/contents/terraform/application.tf")
    assert "sha" not in tf_put[2]["json"]


@pytest.mark.asyncio
async def test_branch_already_exists_self_heals_by_deleting_and_recreating():
    # Regression guard for bug #2's flip side: a stray branch from an
    # earlier failed attempt (or any other reason) must not permanently
    # block every future retry of this job.
    responses = [
        _FakeResponse(200, {"object": {"sha": "base-sha-123"}}),
        _FakeResponse(422, text="Reference already exists"),  # first create attempt
        _FakeResponse(204, {}),  # DELETE the stray branch
        _FakeResponse(201, {}),  # retry create - succeeds
        _FakeResponse(404, text="Not Found"),
        _FakeResponse(201, {}),
        _FakeResponse(404, text="Not Found"),
        _FakeResponse(201, {}),
        _FakeResponse(201, {"html_url": "https://github.com/my-org/my-repo/pull/9", "number": 9}),
    ]
    fake_client = _FakeAsyncClient(responses)
    with patch("httpx.AsyncClient", return_value=fake_client):
        result = await create_adoption_pr(**_base_kwargs())

    assert result["pr_number"] == 9
    methods_and_paths = [(c[0], c[1]) for c in fake_client.calls]
    assert ("DELETE", "/repos/my-org/my-repo/git/refs/heads/terraagent/adopt-job-abc123def456") in methods_and_paths


@pytest.mark.asyncio
async def test_failed_commit_deletes_the_branch_before_raising():
    # Regression guard for bug #2: a mid-sequence failure must not leave a
    # half-populated branch behind - the next retry of this exact job needs
    # to be able to start clean.
    responses = [
        _FakeResponse(200, {"object": {"sha": "base-sha-123"}}),
        _FakeResponse(201, {}),  # branch created
        _FakeResponse(404, text="Not Found"),
        _FakeResponse(422, text="Invalid request - sha mismatch"),  # PUT fails
        _FakeResponse(204, {}),  # cleanup DELETE
    ]
    fake_client = _FakeAsyncClient(responses)
    with patch("httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(GitHubPullRequestError):
            await create_adoption_pr(**_base_kwargs())

    methods_and_paths = [(c[0], c[1]) for c in fake_client.calls]
    assert methods_and_paths[-1] == ("DELETE", "/repos/my-org/my-repo/git/refs/heads/terraagent/adopt-job-abc123def456")


@pytest.mark.asyncio
async def test_failed_pr_creation_also_deletes_the_branch():
    responses = [
        _FakeResponse(200, {"object": {"sha": "base-sha-123"}}),
        _FakeResponse(201, {}),
        _FakeResponse(404, text="Not Found"),
        _FakeResponse(201, {}),
        _FakeResponse(404, text="Not Found"),
        _FakeResponse(201, {}),
        _FakeResponse(403, text="Forbidden - insufficient permissions"),  # PR open fails
        _FakeResponse(204, {}),  # cleanup DELETE
    ]
    fake_client = _FakeAsyncClient(responses)
    with patch("httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(GitHubPullRequestError):
            await create_adoption_pr(**_base_kwargs())

    assert fake_client.calls[-1][0] == "DELETE"


@pytest.mark.asyncio
async def test_missing_base_branch_raises_without_leaking_token():
    responses = [_FakeResponse(404, text="Branch not found")]
    fake_client = _FakeAsyncClient(responses)
    with patch("httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(GitHubPullRequestError) as exc_info:
            await create_adoption_pr(**_base_kwargs())

    assert FAKE_TOKEN not in str(exc_info.value)


@pytest.mark.asyncio
async def test_commit_failure_raises_and_never_leaks_token():
    responses = [
        _FakeResponse(200, {"object": {"sha": "base-sha-123"}}),
        _FakeResponse(201, {}),
        _FakeResponse(404, text="Not Found"),
        _FakeResponse(422, text="Invalid request - sha mismatch"),  # first PUT fails
        _FakeResponse(204, {}),  # cleanup DELETE
    ]
    fake_client = _FakeAsyncClient(responses)
    with patch("httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(GitHubPullRequestError) as exc_info:
            await create_adoption_pr(**_base_kwargs())

    assert FAKE_TOKEN not in str(exc_info.value)
    assert "application.tf" in str(exc_info.value)


@pytest.mark.asyncio
async def test_pr_body_includes_adoption_plan_equivalence_and_security_findings():
    responses = [
        _FakeResponse(200, {"object": {"sha": "base-sha-123"}}),
        _FakeResponse(201, {}),
        _FakeResponse(404, text="Not Found"),
        _FakeResponse(201, {}),
        _FakeResponse(404, text="Not Found"),
        _FakeResponse(201, {}),
        _FakeResponse(201, {"html_url": "https://github.com/my-org/my-repo/pull/9", "number": 9}),
    ]
    fake_client = _FakeAsyncClient(responses)
    kwargs = _base_kwargs(
        adoption_plan={"total_resource_count": 4, "risk_score": 33, "summary": "Adopts a small VPC."},
        plan_equivalence_results={"create": 4, "update": 0, "replace": 0, "destroy": 0, "no_op": 0, "skipped": False},
        security_results={
            "risk_score": 40, "critical_count": 0, "high_count": 1,
            "findings": [{"tool": "tfsec", "rule_id": "AVD-AWS-0080", "severity": "HIGH", "resource": "aws_db_instance.x"}],
        },
        pending_approval={
            "reason": "plan_equivalence_requires_human_approval",
            "findings": [{"tier": "destructive", "resource": "aws_iam_role.y", "description": "destroy action"}],
        },
        approval_decision={"decision": "approved", "reason": "reviewed and safe", "decided_at": "2026-01-01T00:00:00"},
    )
    with patch("httpx.AsyncClient", return_value=fake_client):
        await create_adoption_pr(**kwargs)

    pr_call = next(c for c in fake_client.calls if c[0] == "POST" and c[1].endswith("/pulls"))
    body = pr_call[2]["json"]["body"]

    assert "Adopts a small VPC." in body
    assert "create: 4" in body
    assert "AVD-AWS-0080" in body
    assert "aws_iam_role.y" in body
    assert "APPROVED" in body
    assert "reviewed and safe" in body


@pytest.mark.asyncio
async def test_wave_scoped_pr_only_commits_that_waves_resource_blocks():
    bucket_name = unique_clean_name("b", "bucket-1")
    instance_name = unique_clean_name("i", "instance-1")
    tf_files = {
        "application.tf": (
            f'resource "aws_s3_bucket" "{bucket_name}" {{\n  bucket = "b"\n}}\n\n'
            f'resource "aws_instance" "{instance_name}" {{\n  ami = "x"\n}}\n'
        ),
        "providers.tf": 'provider "aws" {\n  region = "us-east-1"\n}\n',
    }
    resources = [
        {"id": "bucket-1", "resource_type": "aws_s3_bucket", "name": "b"},
        {"id": "instance-1", "resource_type": "aws_instance", "name": "i"},
    ]
    wave = {"wave": 1, "resource_ids": ["bucket-1"], "risk_level": "low", "risk_signals": []}

    responses = [
        _FakeResponse(200, {"object": {"sha": "base-sha-123"}}),
        _FakeResponse(201, {}),
        _FakeResponse(404, text="Not Found"),  # application.tf new on this branch
        _FakeResponse(201, {}),  # PUT terraform/application.tf
        _FakeResponse(404, text="Not Found"),  # providers.tf new on this branch
        _FakeResponse(201, {}),  # PUT terraform/providers.tf
        _FakeResponse(201, {"html_url": "https://github.com/my-org/my-repo/pull/11", "number": 11}),
    ]
    fake_client = _FakeAsyncClient(responses)
    kwargs = _base_kwargs(tf_files=tf_files, resources=resources, wave=wave)
    with patch("httpx.AsyncClient", return_value=fake_client):
        result = await create_adoption_pr(**kwargs)

    assert result["branch"] == "terraagent/adopt-job-abc123def456-wave-1"
    assert result["wave"] == 1

    put_calls = [c for c in fake_client.calls if c[0] == "PUT"]
    app_tf_put = next(c for c in put_calls if c[1] == "/repos/my-org/my-repo/contents/terraform/application.tf")
    committed_content = base64.b64decode(app_tf_put[2]["json"]["content"]).decode()
    assert bucket_name in committed_content
    assert instance_name not in committed_content  # wave 1 only covers bucket-1

    # Shared, non-per-resource files still get committed wholesale - a
    # wave's extracted blocks aren't valid standalone HCL without them.
    assert any(c[1] == "/repos/my-org/my-repo/contents/terraform/providers.tf" for c in put_calls)

    # docs (README.md/import_plan.md describe the WHOLE job) are
    # deliberately not committed for a wave-scoped PR.
    assert not any("README.md" in c[1] for c in put_calls)

    pr_call = next(c for c in fake_client.calls if c[0] == "POST" and c[1].endswith("/pulls"))
    assert "wave 1" in pr_call[2]["json"]["title"].lower()
    body = pr_call[2]["json"]["body"]
    assert "Wave 1" in body
    assert f"terraform import aws_s3_bucket.{bucket_name} bucket-1" in body
    # instance-1 isn't part of this wave - its import command must not leak in
    assert instance_name not in body


@pytest.mark.asyncio
async def test_wave_without_resources_raises_value_error():
    wave = {"wave": 1, "resource_ids": ["bucket-1"], "risk_level": "low", "risk_signals": []}
    with pytest.raises(ValueError):
        await create_adoption_pr(**_base_kwargs(wave=wave, resources=None))
