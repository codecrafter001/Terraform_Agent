"""Pydantic models for deployment mode (routers/deployments.py)."""

import re
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

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


class GitHubUpdateRequest(BaseModel):
    """New source for a deployed app from the GitHub repository it was deployed from."""
    ref: Optional[str] = Field(default=None, description="Branch, tag or commit SHA; the originally deployed ref when empty")
    github_token: Optional[SecretStr] = Field(
        default=None,
        description="Only for private repositories (Contents: Read-only). Used once for the download, never stored.",
    )


class StaticSiteSettings(BaseModel):
    price_class: Literal["PriceClass_100", "PriceClass_200", "PriceClass_All"] = "PriceClass_100"
    spa_mode: bool = False


class LambdaSettings(BaseModel):
    memory_mb: int = Field(default=256, ge=128, le=10240)
    timeout_s: int = Field(default=30, ge=1, le=900)
    public_url: bool = True


_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_HEALTH_PATH = re.compile(r"^/[A-Za-z0-9_./-]{0,200}$")
_FARGATE_CPU = (256, 512, 1024, 2048, 4096)
_FARGATE_MEMORY = (512, 1024, 2048, 4096, 8192)


def _check_cpu(v: int) -> int:
    if v not in _FARGATE_CPU:
        raise ValueError(f"cpu must be one of {_FARGATE_CPU}")
    return v


def _check_memory(v: int) -> int:
    if v not in _FARGATE_MEMORY:
        raise ValueError(f"memory_mb must be one of {_FARGATE_MEMORY}")
    return v


def _check_health_path(v: str) -> str:
    if not _HEALTH_PATH.match(v):
        raise ValueError("health_check_path must start with / and contain only URL path characters")
    return v


class EcsSettings(BaseModel):
    container_port: int = Field(default=8080, ge=1, le=65535)
    cpu: int = Field(default=256)
    memory_mb: int = Field(default=512)
    desired_count: int = Field(default=1, ge=1, le=10)
    # Empty = the content-addressed tag of the build's source.zip (what CodeBuild pushes).
    image_tag: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    health_check_path: str = "/"
    certificate_arn: Optional[str] = None

    _cpu = field_validator("cpu")(_check_cpu)
    _memory = field_validator("memory_mb")(_check_memory)
    _health = field_validator("health_check_path")(_check_health_path)


class FullstackSettings(BaseModel):
    preset: Optional[Literal["dev", "staging", "production"]] = Field(
        default=None, description="The preset the settings started from (deploy/estimates.py::PRESETS); kept for the record")
    cdn_enabled: bool = Field(default=True, description="CloudFront in front of the app; always on with a separate frontend")
    container_port: Optional[int] = Field(default=None, ge=1, le=65535, description="Empty = the detected port")
    cpu: int = Field(default=256)
    memory_mb: int = Field(default=512)
    desired_count: int = Field(default=1, ge=1, le=10)
    health_check_path: str = "/"
    price_class: Literal["PriceClass_100", "PriceClass_200", "PriceClass_All"] = "PriceClass_100"
    database: Literal["rds", "aurora", "external", "none"] = Field(
        default="rds",
        description="rds: PostgreSQL/MySQL on RDS; aurora: Aurora Serverless v2; external: an empty DATABASE_URL "
                    "secret for a database you host; none",
    )
    aurora_min_acu: float = Field(default=0.5, description="Aurora Serverless v2 minimum capacity; 0 pauses when idle")
    aurora_max_acu: float = Field(default=4, ge=1, le=128)
    # Add-ons: None follows what the analyzer detected (deploy/fullstack.py), True/False overrides it.
    cache: Optional[Literal["valkey", "none"]] = Field(default=None, description="ElastiCache Serverless (Valkey) for REDIS_URL")
    cache_max_gb: int = Field(default=1, ge=1, le=100)
    uploads_bucket: Optional[bool] = Field(default=None, description="A private S3 bucket the app can read and write")
    worker_enabled: Optional[bool] = Field(default=None, description="Run the detected background worker as a second service")
    autoscaling_max_count: Optional[int] = Field(default=None, ge=1, le=20, description="Empty = no autoscaling (fixed desired_count)")
    autoscaling_cpu_target: int = Field(default=60, ge=20, le=90)
    db_instance_class: Literal["db.t4g.micro", "db.t4g.small", "db.t4g.medium", "db.t4g.large", "db.m7g.large"] = "db.t4g.micro"
    db_allocated_storage_gb: int = Field(default=20, ge=20, le=500)
    db_multi_az: bool = False
    db_backup_retention_days: int = Field(default=7, ge=0, le=35)
    db_final_snapshot: bool = True
    run_migrations: bool = Field(default=True, description="Run the detected schema command before the server starts")
    secret_env_keys: Optional[List[str]] = Field(
        default=None, max_length=30,
        description="Environment variables that get an empty Secrets Manager secret; empty = the detected ones",
    )

    _cpu = field_validator("cpu")(_check_cpu)
    _memory = field_validator("memory_mb")(_check_memory)
    _health = field_validator("health_check_path")(_check_health_path)

    @field_validator("aurora_min_acu")
    @classmethod
    def _min_acu(cls, v: float) -> float:
        if v not in (0, 0.5, 1, 2, 4, 8, 16):
            raise ValueError("aurora_min_acu must be 0, 0.5, 1, 2, 4, 8 or 16")
        return v

    @model_validator(mode="after")
    def _ranges(self) -> "FullstackSettings":
        if self.autoscaling_max_count is not None and self.autoscaling_max_count < self.desired_count:
            raise ValueError("autoscaling_max_count can't be lower than desired_count")
        if self.aurora_max_acu < max(self.aurora_min_acu, 1):
            raise ValueError("aurora_max_acu must be at least aurora_min_acu (and at least 1)")
        return self

    @field_validator("secret_env_keys")
    @classmethod
    def _keys(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return None
        for key in v:
            if not _ENV_KEY.match(key):
                raise ValueError(f"'{key[:40]}' isn't an UPPER_CASE environment variable name")
            if key.startswith("AWS_"):
                raise ValueError("AWS_* variables can't be secrets: the container uses its IAM task role")
        return sorted(set(v))


class PrepareStaticSite(BaseModel):
    target: Literal["static_site"]
    settings: StaticSiteSettings = StaticSiteSettings()


class PrepareLambda(BaseModel):
    target: Literal["lambda_http"]
    settings: LambdaSettings = LambdaSettings()


class PrepareEcs(BaseModel):
    target: Literal["ecs_service"]
    settings: EcsSettings = EcsSettings()


class PrepareFullstack(BaseModel):
    target: Literal["fullstack_app"]
    settings: FullstackSettings = FullstackSettings()


PrepareRequest = Annotated[Union[PrepareStaticSite, PrepareLambda, PrepareEcs, PrepareFullstack], Field(discriminator="target")]


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
    can_update_code: bool = False
    code_update: Optional[Dict[str, Any]] = None



class RenderedTerraformResponse(BaseModel):
    deployment_id: str
    files: Dict[str, str]


