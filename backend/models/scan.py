"""Pydantic Models for Scan requests, responses, and status definitions."""

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, SecretStr


class OperationType(str, Enum):
    GENERATE = "generate"
    SCAN = "scan"
    MODIFY = "modify"
    EXPLAIN = "explain"
    FIX = "fix"
    VALIDATE = "validate"


class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    # Set when plan_equivalence_agent or repair_agent escalates a destructive/
    # behavior_changing finding - the pipeline halts here (routes to END
    # instead of cost_agent/documentation_agent) until a human calls
    # POST /scan/{job_id}/approve or /reject.
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REJECTED = "REJECTED"


class IntentAnalysisRequest(BaseModel):
    user_request: str = Field(..., description="DevOps natural-language infrastructure request")
    region: str = Field(default="us-east-1", description="Target AWS region")
    environment: str = Field(default="production", description="Target environment (e.g. production, staging, dev)")
    resource_filters: List[str] = Field(
        default_factory=lambda: ["EC2", "VPC", "S3", "RDS", "IAM", "SG", "ELB", "DYNAMODB", "KMS", "SQS", "SNS"],
        description="Filter specific resource categories"
    )


class IntentAnalysisResponse(BaseModel):
    operation: str
    operation_label: str
    target_resources: List[Dict[str, Any]] = []
    requested_changes: List[Dict[str, Any]] = []
    confidence_score: float = 0.95
    summary: str
    environment: str
    region: str
    risk_level: str = "low"
    suggested_filters: List[str] = []


class ScanRequest(BaseModel):
    aws_access_key: SecretStr = Field(..., description="AWS Access Key ID (never logged or exposed)")
    aws_secret_key: SecretStr = Field(..., description="AWS Secret Access Key (never logged or exposed)")
    aws_session_token: Optional[SecretStr] = Field(None, description="Optional AWS Session Token for STS assumed roles")
    region: str = Field(default="us-east-1", description="Target AWS region, or \"auto\" to let Resource Explorer pick")
    environment: Optional[str] = Field(default="production", description="Target environment name")
    user_request: Optional[str] = Field(None, description="Optional natural-language DevOps change or generation request")
    analyzed_intent: Optional[Dict[str, Any]] = Field(None, description="Pre-analyzed structured intent confirmed by user")
    use_resource_explorer: bool = Field(
        default=True,
        description="Query AWS Resource Explorer (read-only) for an all-region inventory before discovery",
    )
    operation: OperationType = Field(default=OperationType.GENERATE, description="Pipeline operation mode")
    resource_filters: List[str] = Field(
        default_factory=lambda: ["EC2", "VPC", "S3", "RDS", "IAM", "SG", "ELB", "DYNAMODB", "KMS", "SQS", "SNS"],
        description="Filter specific resource categories to scan"
    )
    role_arn: Optional[str] = Field(
        None,
        description="Optional IAM role ARN to assume via STS for multi-account access. "
                     "aws_access_key/aws_secret_key are used only to call sts:AssumeRole; "
                     "the resulting temporary credentials (never the long-lived keys) are "
                     "what actually scans the target account, and are discarded at the end "
                     "of the scan - never persisted beyond the scan session."
    )
    external_id: Optional[str] = Field(
        None,
        min_length=2,
        max_length=1224,
        pattern=r"^[\w+=,.@:/-]+$",
        description="ExternalId the target role's trust policy requires (per tenant) - "
                    "passed to sts:AssumeRole with role_arn. See docs/aws/read-only-role.md.",
    )
    webhook_url: Optional[str] = Field(
        None, description="Optional URL to POST a scan result summary to on completion"
    )
    zip_password: Optional[SecretStr] = Field(
        None, description="Optional password to AES-256-encrypt the output ZIP bundle with"
    )
    terraform_binary: Literal["terraform", "tofu"] = Field(
        default="terraform",
        description="IaC engine to format/validate generated code with - Terraform or OpenTofu. "
                     "Both are drop-in compatible CLIs, so this only changes which binary "
                     "tools/terraform_runner.py shells out to, never the generated HCL itself."
    )
    run_plan_equivalence: bool = Field(
        default=False,
        description="Opt-in: after validation, run a REAL `terraform init && plan` against live "
                     "AWS (using aws_access_key/aws_secret_key, scoped to that one subprocess "
                     "call only) to prove the generated configuration doesn't imply replacing or "
                     "destroying anything. Off by default because, unlike every other check in "
                     "this pipeline, it requires a real provider handshake against your AWS "
                     "account rather than a read-only boto3 call or an offline schema check."
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "aws_access_key": "AKIAXXXXXXXXXXXXXXXX",
                "aws_secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "region": "us-east-1",
                "environment": "production",
                "user_request": "Increase EC2 web server from t2.micro to t2.medium and scale Fargate from 2 to 4 tasks.",
                "operation": "modify",
                "resource_filters": ["EC2", "VPC", "S3", "RDS", "IAM", "SG", "ELB", "DYNAMODB", "KMS", "SQS", "SNS"]
            }
        }
    }


class ApprovalActionRequest(BaseModel):
    reason: Optional[str] = Field(
        None, description="Optional human-readable note explaining this approve/reject decision"
    )
    resource_decisions: Dict[str, Literal["manage", "reference", "exclude"]] = Field(
        default_factory=dict,
        description="Approve only: a decision for every resource the gate listed as in Review",
    )
    # Credentials are never stored, so a run resumed after Review decisions
    # that add code can only redo live-AWS checks if they are supplied again.
    # Used for that resumed run only, then discarded like the originals.
    aws_access_key: Optional[SecretStr] = None
    aws_secret_key: Optional[SecretStr] = None
    aws_session_token: Optional[SecretStr] = None


class CreatePullRequestRequest(BaseModel):
    github_token: SecretStr = Field(
        ...,
        description="GitHub personal access token (or fine-grained token) with contents:write "
                     "and pull_requests:write on the target repo. Supplied only at PR-creation "
                     "time, after the scan has already completed - never persisted, never logged, "
                     "and never added to pipeline state; used once in-memory for this request only."
    )
    repo: str = Field(..., description="Target repository in 'owner/repo' form")
    base_branch: str = Field(default="main", description="Branch to open the pull request against")
    wave: Optional[int] = Field(
        default=None,
        description="Scope this PR to a single adoption-plan wave (1-indexed, matching "
                     "adoption_plan.waves[].wave) instead of the whole job - a smaller, more "
                     "reviewable diff containing only that wave's resource blocks plus shared "
                     "foundational files. Omit for the original whole-job PR."
    )
    kind: Literal["adoption", "hardening"] = Field(
        default="adoption",
        description="adoption: the zero-change import PR. hardening: the optional security-fix PR, "
                    "stacked on the adoption PR's branch (open the adoption PR first).",
    )


class MergePullRequestRequest(BaseModel):
    """Merge one of THIS job's own PRs - never an arbitrary repo/PR. Merging
    can trigger the team's pipeline to apply (Atlantis/HCP Terraform
    auto-apply), so it needs an explicit confirmation, a human approval done
    in GitHub, and a mergeable PR (routers/scan.py::merge_pull_request)."""
    github_token: SecretStr = Field(
        ...,
        description="GitHub token with contents:write and pull_requests:write. Used once, never stored."
    )
    kind: Literal["adoption", "hardening"] = Field(
        default="adoption", description="Which of this job's PRs to merge"
    )
    confirm: bool = Field(
        default=False,
        description="Must be true: the caller confirms that merging may make their pipeline apply the change.",
    )
    merge_method: Literal["squash", "merge", "rebase"] = Field(
        default="squash",
        description="Git merge method to use (squash, merge, or rebase)"
    )
    commit_title: Optional[str] = Field(None, max_length=250, description="Optional custom commit title")
    commit_message: Optional[str] = Field(None, max_length=5000, description="Optional custom commit message")


class PullRequestFileResponse(BaseModel):
    filename: str
    status: Optional[str] = None
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    patch: Optional[str] = None
    raw_url: Optional[str] = None


class PullRequestReviewResponse(BaseModel):
    id: Optional[int] = None
    user: Optional[str] = None
    state: str
    submitted_at: Optional[str] = None
    body: Optional[str] = None


class PullRequestDetailsResponse(BaseModel):
    job_id: str
    repo: str
    pr_number: int
    title: str
    state: str
    html_url: str
    body: Optional[str] = None
    head_branch: str
    base_branch: str
    head_sha: Optional[str] = None
    mergeable: Optional[bool] = None
    mergeable_state: Optional[str] = None
    merged: bool = False
    merged_at: Optional[str] = None
    merge_commit_sha: Optional[str] = None
    additions: int = 0
    deletions: int = 0
    changed_files_count: int = 0
    changed_files: List[PullRequestFileResponse] = []
    reviews: List[PullRequestReviewResponse] = []
    diff: Optional[str] = None
    workflow_runs: List[Dict[str, Any]] = []


class MergePullRequestResponse(BaseModel):
    job_id: str
    merged: bool
    sha: Optional[str] = None
    message: str
    workflow_runs: List[Dict[str, Any]] = []


class ScanResponse(BaseModel):
    job_id: str
    status: JobStatus
    created_at: str
    operation: OperationType
    region: str
    message: Optional[str] = None

