"""Tests for Phase 4B: GitOps GitHub Pull Request Delivery."""

import asyncio
import uuid
from unittest.mock import AsyncMock, patch
import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from starlette.requests import Request

from deploy.executor import GitOpsPrExecutor
from deploy.store import DeployStatus, create_deployment, get_deployment, transition
from models.deployment import CreatePullRequestRequest, MergePullRequestRequest
import routers.deployments as api
from services.database import init_db


def _request(headers: list = None) -> Request:
    raw_headers = [(b"x-forwarded-email", b"lead-devops@example.com")] if headers is None else headers
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": raw_headers,
        "query_string": b"",
    })


@pytest.fixture(autouse=True)
def setup_db():
    init_db()
    yield


@pytest.fixture
def approved_deployment():
    dep_id = f"dep-gitops-{uuid.uuid4().hex[:8]}"
    create_deployment(
        dep_id,
        source_kind="github",
        source_name="acme/my-app",
        region="us-east-1",
        environment="production",
        requested_by="alice@example.com",
    )
    transition(dep_id, DeployStatus.ANALYZING)
    transition(dep_id, DeployStatus.ANALYZED)
    transition(dep_id, DeployStatus.BUILDING)
    transition(dep_id, DeployStatus.VERIFYING)
    transition(dep_id, DeployStatus.VERIFIED)
    transition(dep_id, DeployStatus.PLANNING)
    transition(dep_id, DeployStatus.AWAITING_APPROVAL)

    rendered_files = {
        "main.tf": 'resource "aws_s3_bucket" "b" { bucket = "my-bucket" }',
        "variables.tf": 'variable "region" { default = "us-east-1" }',
        "outputs.tf": 'output "bucket_name" { value = aws_s3_bucket.b.id }',
    }
    plan_summary = {"to_add": 1, "to_change": 0, "to_destroy": 0, "counts": {"create": 1, "update": 0, "destroy": 0}}
    plan_policy = {"verdict": "PASSED", "violations": []}
    verification = {
        "verdict": "PASS",
        "infracost": {"total_monthly_cost": 5.20},
        "security_posture": {"score": 95, "reason": "No high severity issues"},
    }

    transition(
        dep_id,
        DeployStatus.APPROVED,
        actor="lead-devops@example.com",
        reason="Approved for production",
        rendered=rendered_files,
        plan_summary=plan_summary,
        plan_policy=plan_policy,
        verification=verification,
        plan_bundle_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        approved_by="lead-devops@example.com",
        approved_at="2026-10-02T12:00:00Z",
    )
    return dep_id


@pytest.mark.asyncio
async def test_gitops_pr_executor_success(approved_deployment):
    executor = GitOpsPrExecutor(
        github_token="ghp_mocktoken123456789",
        repo="acme/my-app",
        base_branch="main",
        target_dir="terraform",
        add_workflows=True,
    )

    mock_pr_response = {
        "number": 42,
        "html_url": "https://github.com/acme/my-app/pull/42",
        "title": "[TerraAgent] Deploy acme/my-app (aws)",
        "created_at": "2026-10-02T12:05:00Z",
    }

    with patch("deploy.executor._publish", new_callable=AsyncMock) as mock_publish, \
         patch("deploy.executor.redis_service.publish_log", new_callable=AsyncMock) as mock_log:
        mock_publish.return_value = (mock_pr_response, "c0ffee1234567890abcdef")

        res = await executor.execute(approved_deployment)

        assert res["status"] == "PR_OPEN"
        assert res["pr_number"] == 42
        assert res["pr_url"] == "https://github.com/acme/my-app/pull/42"
        assert res["branch"] == f"terraagent/deploy-{approved_deployment[-8:]}"
        assert res["commit_sha"] == "c0ffee1234567890abcdef"

        # Verify _publish was called with expected files including workflows
        mock_publish.assert_called_once()
        _, kwargs = mock_publish.call_args
        files = kwargs["files"]
        assert "terraform/main.tf" in files
        assert "terraform/variables.tf" in files
        assert ".github/workflows/terraagent-plan.yml" in files
        assert ".github/workflows/terraagent-apply.yml" in files

        # Verify PR body contains plan changes, hash, approver
        body = kwargs["pr"]["body"]
        assert "acme/my-app" in body
        assert "lead-devops@example.com" in body
        assert "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" in body
        assert "+1 ~0 -0" in body

        # Verify store updated to PR_OPEN
        dep = get_deployment(approved_deployment)
        assert dep["status"] == "PR_OPEN"
        assert dep["pr"]["number"] == 42
        assert dep["pr"]["html_url"] == "https://github.com/acme/my-app/pull/42"

        # Verify token is NEVER stored in database
        assert "token" not in str(dep)
        assert "ghp_" not in str(dep)


@pytest.mark.asyncio
async def test_gitops_pr_executor_custom_target_dir(approved_deployment):
    executor = GitOpsPrExecutor(
        github_token="ghp_mocktoken123456789",
        repo="acme/my-app",
        base_branch="develop",
        target_dir="infra/aws",
        add_workflows=False,
    )

    mock_pr_response = {
        "number": 43,
        "html_url": "https://github.com/acme/my-app/pull/43",
    }

    with patch("deploy.executor._publish", new_callable=AsyncMock) as mock_publish:
        mock_publish.return_value = (mock_pr_response, "sha987654321")
        await executor.execute(approved_deployment)

        _, kwargs = mock_publish.call_args
        files = kwargs["files"]
        assert "infra/aws/main.tf" in files
        assert ".github/workflows/terraagent-plan.yml" not in files
        assert kwargs["from_branch"] == "develop"


@pytest.mark.asyncio
async def test_gitops_pr_executor_rejects_unapproved():
    dep_id = f"dep-unapproved-{uuid.uuid4().hex[:8]}"
    create_deployment(dep_id, source_kind="zip", source_name="site.zip", region="us-east-1", environment="prod")

    executor = GitOpsPrExecutor(github_token="ghp_test", repo="acme/my-app")
    with pytest.raises(ValueError, match="must be in APPROVED status"):
        await executor.execute(dep_id)


@pytest.mark.asyncio
async def test_api_create_pull_request_endpoint(approved_deployment):
    mock_pr_response = {
        "number": 101,
        "html_url": "https://github.com/acme/my-app/pull/101",
    }

    with patch("deploy.executor._publish", new_callable=AsyncMock) as mock_publish:
        mock_publish.return_value = (mock_pr_response, "sha101")

        body = CreatePullRequestRequest(
            github_token=SecretStr("ghp_supersecretpat"),
            repo="acme/my-app",
            base_branch="main",
            target_dir="terraform",
            add_workflows=True,
        )
        res = await api.create_pull_request_endpoint(_request(), approved_deployment, body)

        assert res["status"] == "PR_OPEN"
        assert res["pr_number"] == 101
        assert res["pr_url"] == "https://github.com/acme/my-app/pull/101"


@pytest.mark.asyncio
async def test_api_get_pull_request_status(approved_deployment):
    # Before PR opened -> 404
    with pytest.raises(HTTPException) as exc:
        await api.get_pull_request_status_endpoint(approved_deployment)
    assert exc.value.status_code == 404

    # Manually transition to PR_OPEN with metadata
    transition(
        approved_deployment,
        DeployStatus.PR_OPEN,
        actor="gitops",
        reason="Opened PR",
        pr={"number": 101, "html_url": "https://github.com/acme/my-app/pull/101", "repo": "acme/my-app"},
    )

    data = await api.get_pull_request_status_endpoint(approved_deployment)
    assert data["pr"]["number"] == 101
    assert data["status"] == "PR_OPEN"


@pytest.mark.asyncio
async def test_api_merge_pull_request_endpoint(approved_deployment):
    # Set to PR_OPEN
    transition(
        approved_deployment,
        DeployStatus.PR_OPEN,
        actor="gitops",
        reason="Opened PR",
        pr={"number": 101, "html_url": "https://github.com/acme/my-app/pull/101", "repo": "acme/my-app"},
    )

    with patch("services.github_client.merge_pr", new_callable=AsyncMock) as mock_merge:
        mock_merge.return_value = {
            "merged": True,
            "sha": "mergecommitsha123",
            "message": "Pull Request successfully merged",
        }

        body = MergePullRequestRequest(
            github_token=SecretStr("ghp_mergetoken"),
            merge_method="squash",
        )
        data = await api.merge_pull_request_endpoint(_request(), approved_deployment, body)

        assert data["status"] == "MERGED"
        assert data["merged"] is True
        assert data["sha"] == "mergecommitsha123"

        # Verify deployment record is MERGED
        dep = get_deployment(approved_deployment)
        assert dep["status"] == "MERGED"
        assert dep["completed_at"] is not None


@pytest.mark.asyncio
async def test_api_merge_pull_request_conflict_or_error(approved_deployment):
    # Attempt merge when not PR_OPEN -> 409
    body = MergePullRequestRequest(github_token=SecretStr("ghp_mergetoken"))
    with pytest.raises(HTTPException) as exc:
        await api.merge_pull_request_endpoint(_request(), approved_deployment, body)
    assert exc.value.status_code == 409
