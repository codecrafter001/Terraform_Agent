"""Unit tests for services/github_client.py - httpx.AsyncClient is replaced
by a fake GitHub that answers by method + path, so these run without a real
token or repo.

What they guard:
- a PR is ONE commit built with the Git Data API (blobs -> tree -> commit),
  and the branch only appears once that commit exists;
- a failure before the branch exists leaves nothing behind, a failure
  opening the PR deletes the branch, and a stray branch from an earlier
  attempt is deleted and recreated - retries always start clean;
- the token never appears in an error or anything sent besides the header.
"""

import base64
from typing import Any, Dict, List, Optional, Tuple
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


class FakeGitHub:
    """Minimal GitHub REST fake. `fail` maps (METHOD, path-substring) to a
    status code; the first matching entry wins. `stray_branch` makes the
    first branch create return 422 (a branch left by an earlier attempt)."""

    def __init__(self, fail: Optional[Dict[Tuple[str, str], int]] = None, stray_branch: bool = False,
                 pr_number: int = 7):
        self.fail = fail or {}
        self.stray_branch = stray_branch
        self.pr_number = pr_number
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.blobs: List[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def _handle(self, method: str, path: str, **kwargs) -> _FakeResponse:
        self.calls.append((method, path, kwargs))
        for (m, fragment), status in self.fail.items():
            if m == method and fragment in path:
                return _FakeResponse(status, text=f"{status} failure on {path}")
        if method == "GET" and "/git/ref/heads/" in path:
            return _FakeResponse(200, {"object": {"sha": "base-sha-123"}})
        if method == "GET" and "/git/commits/" in path:
            return _FakeResponse(200, {"tree": {"sha": "base-tree"}})
        if method == "POST" and path.endswith("/git/blobs"):
            self.blobs.append(base64.b64decode(kwargs["json"]["content"]).decode())
            return _FakeResponse(201, {"sha": f"blob-{len(self.blobs)}"})
        if method == "POST" and path.endswith("/git/trees"):
            return _FakeResponse(201, {"sha": "new-tree"})
        if method == "POST" and path.endswith("/git/commits"):
            return _FakeResponse(201, {"sha": "new-commit"})
        if method == "POST" and path.endswith("/git/refs"):
            if self.stray_branch:
                self.stray_branch = False
                return _FakeResponse(422, text="Reference already exists")
            return _FakeResponse(201, {})
        if method == "DELETE":
            return _FakeResponse(204, {})
        if method == "POST" and path.endswith("/pulls"):
            n = self.pr_number
            return _FakeResponse(201, {"html_url": f"https://github.com/my-org/my-repo/pull/{n}", "number": n,
                                       "state": "open"})
        return _FakeResponse(404, text="Not Found")

    async def get(self, path, **kwargs):
        return await self._handle("GET", path, **kwargs)

    async def post(self, path, **kwargs):
        return await self._handle("POST", path, **kwargs)

    async def put(self, path, **kwargs):
        return await self._handle("PUT", path, **kwargs)

    async def delete(self, path, **kwargs):
        return await self._handle("DELETE", path, **kwargs)

    def paths(self, method: str) -> List[str]:
        return [c[1] for c in self.calls if c[0] == method]

    def tree(self) -> Dict[str, str]:
        """path -> content of the one commit's tree."""
        call = next(c for c in self.calls if c[1].endswith("/git/trees"))
        entries = call[2]["json"]["tree"]
        return {e["path"]: self.blobs[int(e["sha"].split("-")[1]) - 1] for e in entries}


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


async def _run(fake: FakeGitHub, **overrides):
    with patch("httpx.AsyncClient", return_value=fake):
        return await create_adoption_pr(**_base_kwargs(**overrides))


BRANCH = "/repos/my-org/my-repo/git/refs/heads/terraagent/adopt-job-abc123def456"


@pytest.mark.asyncio
async def test_one_commit_with_every_file_then_branch_then_pr():
    fake = FakeGitHub()
    result = await _run(fake)

    assert result["pr_url"] == "https://github.com/my-org/my-repo/pull/7" and result["pr_number"] == 7
    assert result["branch"] == "terraagent/adopt-job-abc123def456" and result["commit_sha"] == "new-commit"
    assert fake.tree() == {"terraform/application.tf": 'resource "aws_s3_bucket" "b" {}', "README.md": "# Bundle"}
    commit = next(c for c in fake.calls if c[1].endswith("/git/commits") and c[0] == "POST")
    assert commit[2]["json"]["parents"] == ["base-sha-123"] and commit[2]["json"]["tree"] == "new-tree"
    ref = next(c for c in fake.calls if c[1].endswith("/git/refs"))
    assert ref[2]["json"] == {"ref": "refs/heads/terraagent/adopt-job-abc123def456", "sha": "new-commit"}
    order = [c[1].rsplit("/", 1)[-1] for c in fake.calls if c[0] == "POST"]
    assert order.index("commits") < order.index("refs") < order.index("pulls")
    assert fake.paths("PUT") == []  # no per-file Contents API commits


@pytest.mark.asyncio
async def test_existing_repo_files_are_simply_replaced_in_the_tree():
    # README.md exists in almost every repo; the new tree just replaces it -
    # built on the base tree, so everything else in the repo is kept.
    fake = FakeGitHub()
    await _run(fake)
    tree_call = next(c for c in fake.calls if c[1].endswith("/git/trees"))
    assert tree_call[2]["json"]["base_tree"] == "base-tree"


@pytest.mark.asyncio
async def test_stray_branch_from_an_earlier_attempt_is_replaced():
    fake = FakeGitHub(stray_branch=True)
    result = await _run(fake)
    assert result["pr_number"] == 7
    assert BRANCH in fake.paths("DELETE")


@pytest.mark.parametrize("failing", [("POST", "/git/blobs"), ("POST", "/git/trees"), ("POST", "/git/commits")])
@pytest.mark.asyncio
async def test_a_failed_upload_leaves_no_branch_behind(failing):
    fake = FakeGitHub(fail={failing: 422})
    with pytest.raises(GitHubPullRequestError) as exc:
        await _run(fake)
    # the branch is only created from a finished commit - never reached here
    assert not any(c[1].endswith("/git/refs") for c in fake.calls)
    assert FAKE_TOKEN not in str(exc.value)


@pytest.mark.asyncio
async def test_failed_upload_names_the_file():
    fake = FakeGitHub(fail={("POST", "/git/blobs"): 422})
    with pytest.raises(GitHubPullRequestError) as exc:
        await _run(fake)
    assert "application.tf" in str(exc.value)


@pytest.mark.asyncio
async def test_failed_pr_creation_deletes_the_branch():
    fake = FakeGitHub(fail={("POST", "/pulls"): 403})
    with pytest.raises(GitHubPullRequestError):
        await _run(fake)
    assert fake.calls[-1] [0] == "DELETE" and fake.calls[-1][1] == BRANCH


@pytest.mark.asyncio
async def test_missing_base_branch_raises_without_leaking_token():
    fake = FakeGitHub(fail={("GET", "/git/ref/heads/"): 404})
    with pytest.raises(GitHubPullRequestError) as exc:
        await _run(fake)
    assert FAKE_TOKEN not in str(exc.value)
    assert FAKE_TOKEN not in str(fake.calls)


@pytest.mark.asyncio
async def test_pr_body_includes_adoption_plan_equivalence_and_security_findings():
    fake = FakeGitHub(pr_number=9)
    await _run(
        fake,
        adoption_plan={"total_resource_count": 4, "risk_score": 33, "summary": "Adopts a small VPC."},
        plan_equivalence_results={"create": 4, "update": 0, "replace": 0, "destroy": 0, "no_op": 0, "skipped": False},
        security_results={
            "risk_score": 40, "critical_count": 0, "high_count": 1,
            "findings": [{"tool": "trivy", "rule_id": "AVD-AWS-0080", "severity": "HIGH", "resource": "aws_db_instance.x"}],
        },
        pending_approval={
            "reason": "plan_equivalence_requires_human_approval",
            "findings": [{"tier": "destructive", "resource": "aws_iam_role.y", "description": "destroy action"}],
        },
        approval_decision={"decision": "approved", "reason": "reviewed and safe", "decided_at": "2026-01-01T00:00:00"},
    )
    body = next(c for c in fake.calls if c[1].endswith("/pulls"))[2]["json"]["body"]
    for expected in ("Adopts a small VPC.", "create: 4", "AVD-AWS-0080", "aws_iam_role.y", "APPROVED", "reviewed and safe"):
        assert expected in body


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
    fake = FakeGitHub(pr_number=11)
    result = await _run(fake, tf_files=tf_files, resources=resources, wave=wave)

    assert result["branch"] == "terraagent/adopt-job-abc123def456-wave-1" and result["wave"] == 1
    tree = fake.tree()
    assert bucket_name in tree["terraform/application.tf"]
    assert instance_name not in tree["terraform/application.tf"]  # wave 1 only covers bucket-1
    # Shared, non-per-resource files still ship - a wave's blocks aren't valid HCL without them.
    assert "terraform/providers.tf" in tree
    # Job-wide docs describe the WHOLE adoption, so a wave PR leaves them out.
    assert "README.md" not in tree

    pr = next(c for c in fake.calls if c[1].endswith("/pulls"))[2]["json"]
    assert "wave 1" in pr["title"].lower()
    assert "Wave 1" in pr["body"] and f"terraform import aws_s3_bucket.{bucket_name} bucket-1" in pr["body"]
    assert instance_name not in pr["body"]


@pytest.mark.asyncio
async def test_wave_without_resources_raises_value_error():
    wave = {"wave": 1, "resource_ids": ["bucket-1"], "risk_level": "low", "risk_signals": []}
    with pytest.raises(ValueError):
        await create_adoption_pr(**_base_kwargs(wave=wave, resources=None))


def test_adoption_body_has_scores_resource_table_and_decisions():
    from services.github_client import _build_pr_body
    vpc_address = f"aws_vpc.{unique_clean_name('main', 'vpc-1')}"
    body = _build_pr_body(
        "job-1", {"total_resource_count": 2}, {"checks": [{"check_name": "show", "passed": True}],
                                               "changes": []},
        {"skipped": True}, {"findings": []}, {"skipped": True}, None, None,
        migration_safety={"score": 100, "status": "SAFE", "basis": "plan", "destroy_or_replace": 0},
        security_posture={"score": 70, "rating": "FAIR", "complete": True},
        infra_model={"summary": {"manage": 1, "reference": 0, "exclude": 1, "review": 0}, "records": [
            {"type": "aws_vpc", "id": "vpc-1", "name": "main", "import_id": "vpc-1", "decision": "manage"},
            {"type": "aws_iam_role", "id": "svc|role", "decision": "exclude", "reasons": ["service-linked"]},
        ]},
    )
    assert "Migration Safety:** 100%" in body and "Security Posture:** 70/100" in body
    assert f"| `{vpc_address}` | `vpc-1` | no-op |" in body
    assert "svc\|role" in body  # discovered text can't break the table
    assert "zero changes" in body and "Cost Impact" in body and "HCP Terraform" in body


@pytest.mark.asyncio
async def test_hardening_pr_stacks_on_the_adoption_branch():
    from services.github_client import create_hardening_pr
    fake = FakeGitHub(pr_number=8)
    hardening = {"files": {"data.tf": "x"}, "validated": True,
                 "changes": [{"title": "Block public access", "impact": "behavior_changing",
                              "explanation": "e", "risk": "r", "findings": ["CKV_AWS_53"]}],
                 "cost": {"monthly_delta": 0.0, "currency": "USD"}}
    with patch("httpx.AsyncClient", return_value=fake):
        result = await create_hardening_pr(FAKE_TOKEN, "o/r", "job-1", hardening,
                                           {"branch": "terraagent/adopt-job-1", "pr_number": 7})

    assert result["kind"] == "hardening" and result["branch"] == "terraagent/harden-job-1"
    assert fake.calls[0][1] == "/repos/o/r/git/ref/heads/terraagent/adopt-job-1"  # built on the adoption branch
    assert fake.tree() == {"terraform/data.tf": "x"}
    pr = next(c for c in fake.calls if c[1].endswith("/pulls"))[2]["json"]
    assert pr["base"] == "terraagent/adopt-job-1" and pr["head"] == "terraagent/harden-job-1"
    assert "Merge and apply the adoption PR first" in pr["body"] and "CKV_AWS_53" in pr["body"]
    assert FAKE_TOKEN not in str(fake.calls)
