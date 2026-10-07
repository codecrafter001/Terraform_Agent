"""Pydantic models for AWS Deploy Targets (Phase 3)."""

import re
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator

_REGION = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
_ACCOUNT_ID = re.compile(r"^\d{12}$")
_ROLE_ARN = re.compile(r"^arn:aws[a-z0-9-]*:iam::\d{12}:role/.+$")
_POLICY_ARN = re.compile(r"^arn:aws[a-z0-9-]*:iam::\d{12}:policy/.+$")
_BUCKET_NAME = re.compile(r"^[a-z0-9.-]{3,63}$")


class AwsDeployTargetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Human friendly name for this target account")
    account_id: str = Field(..., description="12-digit AWS Account ID")
    region: str = Field(default="us-east-1", description="Default AWS region for this target")
    plan_role_arn: str = Field(..., description="ARN of TerraAgentDeployPlan role")
    apply_role_arn: str = Field(..., description="ARN of TerraAgentDeployApply role")
    permissions_boundary_arn: Optional[str] = Field(default=None, description="ARN of TerraAgentWorkloadBoundary policy")
    state_bucket: str = Field(..., description="S3 bucket name for remote Terraform state")

    @field_validator("account_id")
    @classmethod
    def _account_id(cls, v: str) -> str:
        v = v.strip()
        if not _ACCOUNT_ID.match(v):
            raise ValueError("account_id must be a 12-digit AWS Account ID")
        return v

    @field_validator("region")
    @classmethod
    def _region(cls, v: str) -> str:
        v = v.strip()
        if not _REGION.match(v):
            raise ValueError("region must be a valid AWS region name")
        return v

    @field_validator("plan_role_arn", "apply_role_arn")
    @classmethod
    def _role_arn(cls, v: str) -> str:
        v = v.strip()
        if not _ROLE_ARN.match(v):
            raise ValueError("Must be a valid IAM role ARN")
        return v

    @field_validator("permissions_boundary_arn")
    @classmethod
    def _boundary_arn(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        v = v.strip()
        if not _POLICY_ARN.match(v):
            raise ValueError("permissions_boundary_arn must be a valid IAM policy ARN")
        return v

    @field_validator("state_bucket")
    @classmethod
    def _state_bucket(cls, v: str) -> str:
        v = v.strip().lower()
        if not _BUCKET_NAME.match(v):
            raise ValueError("state_bucket must be a valid S3 bucket name")
        return v


class AwsDeployTargetResponse(BaseModel):
    id: str
    name: str
    account_id: str
    region: str
    plan_role_arn: str
    apply_role_arn: str
    permissions_boundary_arn: Optional[str] = None
    state_bucket: str
    external_id: str
    owner: Optional[str] = None
    tenant_id: Optional[str] = None
    verified_at: Optional[str] = None
    created_at: str
    updated_at: str


class AwsDeployTargetVerifyResult(BaseModel):
    target_id: str
    verified: bool
    plan_role_ok: bool
    apply_role_ok: bool
    bucket_ok: bool
    caller_identity: Optional[Dict[str, Any]] = None
    message: str
