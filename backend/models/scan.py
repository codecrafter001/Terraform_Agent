"""Pydantic Models for Scan requests, responses, and status definitions."""

from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, SecretStr


class OperationType(str, Enum):
    GENERATE = "generate"
    SCAN = "scan"
    EXPLAIN = "explain"
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


class ScanRequest(BaseModel):
    aws_access_key: SecretStr = Field(..., description="AWS Access Key ID (never logged or exposed)")
    aws_secret_key: SecretStr = Field(..., description="AWS Secret Access Key (never logged or exposed)")
    aws_session_token: Optional[SecretStr] = Field(None, description="Optional AWS Session Token for STS assumed roles")
    region: str = Field(default="us-east-1", description="Target AWS region")
    operation: OperationType = Field(default=OperationType.GENERATE, description="Pipeline operation mode")
    resource_filters: List[str] = Field(
        default_factory=lambda: ["EC2", "VPC", "S3", "RDS", "IAM", "SG"],
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
                "operation": "generate",
                "resource_filters": ["EC2", "VPC", "S3", "RDS", "IAM", "SG"]
            }
        }
    }


class ApprovalActionRequest(BaseModel):
    reason: Optional[str] = Field(
        None, description="Optional human-readable note explaining this approve/reject decision"
    )


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


class ScanResponse(BaseModel):
    job_id: str
    status: JobStatus
    created_at: str
    operation: OperationType
    region: str
    message: Optional[str] = None
