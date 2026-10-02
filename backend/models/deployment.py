"""Pydantic models for deployment mode (routers/deployments.py)."""

import re
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, SecretStr, field_validator

_REGION = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
_ENVIRONMENT = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def check_region(v: str) -> str:
    v = (v or "").strip()
    if not _REGION.match(v):
        raise ValueError("region must be an AWS region name, e.g. us-east-1")
    return v


def check_environment(v: str) -> str:
    v = (v or "").strip() or "production"
    if not _ENVIRONMENT.match(v):
        raise ValueError("environment must be 1-32 letters, digits, '-' or '_'")
    return v


class GitHubSourceRequest(BaseModel):
    repo: str = Field(..., description="owner/repo or a github.com URL")
    ref: Optional[str] = Field(default=None, description="Branch, tag or commit SHA; the default branch when empty")
    github_token: Optional[SecretStr] = Field(
        default=None,
        description="Only for private repositories (Contents: Read-only). Used once for the download, never stored.",
    )
    region: str = "us-east-1"
    environment: str = "production"

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str) -> str:
        from tools.github_repo import normalize_repo
        return normalize_repo(v)

    @field_validator("region")
    @classmethod
    def _region(cls, v: str) -> str:
        return check_region(v)

    @field_validator("environment")
    @classmethod
    def _environment(cls, v: str) -> str:
        return check_environment(v)


class StaticSiteSettings(BaseModel):
    price_class: Literal["PriceClass_100", "PriceClass_200", "PriceClass_All"] = "PriceClass_100"
    spa_mode: bool = False


class LambdaSettings(BaseModel):
    memory_mb: int = Field(default=256, ge=128, le=10240)
    timeout_s: int = Field(default=30, ge=1, le=900)
    public_url: bool = True


class EcsSettings(BaseModel):
    container_port: int = Field(default=8080, ge=1, le=65535)
    cpu: int = Field(default=256)
    memory_mb: int = Field(default=512)
    desired_count: int = Field(default=1, ge=1, le=10)
    image_tag: str = Field(default="latest")
    certificate_arn: Optional[str] = None


class PrepareStaticSite(BaseModel):
    target: Literal["static_site"]
    settings: StaticSiteSettings = StaticSiteSettings()


class PrepareLambda(BaseModel):
    target: Literal["lambda_http"]
    settings: LambdaSettings = LambdaSettings()


class PrepareEcs(BaseModel):
    target: Literal["ecs_service"]
    settings: EcsSettings = EcsSettings()


PrepareRequest = Annotated[Union[PrepareStaticSite, PrepareLambda, PrepareEcs], Field(discriminator="target")]


class PlanRequest(BaseModel):
    target_id: str = Field(..., description="ID of the registered AWS deploy target (Settings -> Deploy Targets)")


class ApprovalRequest(BaseModel):
    plan_bundle_sha256: str = Field(..., description="SHA-256 hash of the exact plan bundle being approved")
    confirm: bool = Field(default=False, description="Explicit confirmation of intent to approve")
    acknowledge_destructive: bool = Field(default=False, description="Explicit acknowledgment if the plan contains replace or delete changes")
    reason: Optional[str] = Field(default=None, description="Optional explanation/justification for the approval")


class RejectionRequest(BaseModel):
    reason: Optional[str] = Field(default=None, description="Optional explanation for rejecting the plan")


class DeployRequest(BaseModel):
    confirm: bool = Field(default=False, description="Explicit confirmation of intent to apply the approved plan to AWS")


class CreatePullRequestRequest(BaseModel):
    github_token: SecretStr = Field(..., description="GitHub PAT with contents=write and pull_requests=write permissions.")
    repo: str = Field(..., description="Target repository in owner/repo format or github.com URL.")
    base_branch: str = Field(default="main", description="Target base branch to open PR against.")
    target_dir: str = Field(default="terraform", description="Subdirectory in repository to place Terraform files.")
    add_workflows: bool = Field(default=False, description="Whether to generate GitHub Actions CI/CD workflows.")

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str) -> str:
        from tools.github_repo import normalize_repo
        return normalize_repo(v)


class MergePullRequestRequest(BaseModel):
    github_token: SecretStr = Field(..., description="GitHub PAT with contents=write and pull_requests=write permissions.")
    merge_method: Literal["squash", "merge", "rebase"] = Field(default="squash", description="Merge method to use.")
    commit_title: Optional[str] = Field(default=None, description="Optional commit title for the merge.")
    commit_message: Optional[str] = Field(default=None, description="Optional commit message for the merge.")


class RollbackRequest(BaseModel):
    target_artifact_id: Optional[str] = Field(default=None, description="Artifact ID of the previous build bundle to roll back to.")
    target_release_id: Optional[str] = Field(default=None, description="Release ID (for static site origin_path switch).")
    reason: Optional[str] = Field(default=None, description="Optional justification for triggering rollback.")


class BuildHistoryItem(BaseModel):
    artifact_id: str
    deployment_id: str
    kind: str
    sha256: str
    size: int
    created_at: str
    release_id: Optional[str] = None


class DeploymentAccepted(BaseModel):
    deployment_id: str
    status: str
    message: str


class DeploymentSummary(BaseModel):
    id: str
    status: str
    source_kind: str
    source_name: str
    region: str
    environment: str
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    plan_bundle_sha256: Optional[str] = None
    plan_kind: Optional[str] = "plan"
    is_destructive: bool = False
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    applied_at: Optional[str] = None
    verdict: Optional[str] = None
    error: Optional[str] = None
    pr: Optional[Dict[str, Any]] = None
    owner: Optional[str] = None
    tenant_id: Optional[str] = None
    created_at: str
    updated_at: str
    completed_at: Optional[str] = None


class DeploymentEventResponse(BaseModel):
    from_status: Optional[str] = None
    to_status: str
    actor: str
    reason: Optional[str] = None
    created_at: str


class DeploymentDetail(DeploymentSummary):
    source_sha256: Optional[str] = None
    requested_by: Optional[str] = None
    intake: Optional[Dict[str, Any]] = None
    profile: Optional[Dict[str, Any]] = None
    decision: Optional[Dict[str, Any]] = None
    settings: Optional[Dict[str, Any]] = None
    build: Optional[Dict[str, Any]] = None
    verification: Optional[Dict[str, Any]] = None
    plan: Optional[Dict[str, Any]] = None
    plan_summary: Optional[Dict[str, Any]] = None
    plan_policy: Optional[Dict[str, Any]] = None
    outputs: Optional[Dict[str, Any]] = None
    approval_reason: Optional[str] = None
    rejection_reason: Optional[str] = None
    rendered_files: List[str] = []
    events: List[DeploymentEventResponse] = []
    can_prepare: bool = False
    can_plan: bool = False
    can_approve: bool = False
    can_deploy: bool = False
    can_open_pr: bool = False
    can_merge_pr: bool = False
    can_rollback: bool = False
    can_destroy: bool = False



class RenderedTerraformResponse(BaseModel):
    deployment_id: str
    files: Dict[str, str]


