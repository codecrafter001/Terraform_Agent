"""Pydantic models for the AWS Organizations account-listing endpoint."""

from typing import List, Optional

from pydantic import BaseModel, Field, SecretStr


class OrganizationAccountsRequest(BaseModel):
    aws_access_key: SecretStr = Field(..., description="AWS Access Key ID (never logged or exposed)")
    aws_secret_key: SecretStr = Field(..., description="AWS Secret Access Key (never logged or exposed)")
    aws_session_token: Optional[SecretStr] = Field(None, description="Optional AWS Session Token")
    management_role_arn: Optional[str] = Field(
        None, description="Role ARN to assume into the Organization's management account, if needed"
    )
    region: str = Field(default="us-east-1", description="Region for the underlying STS/Organizations calls")


class OrganizationAccount(BaseModel):
    account_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    status: Optional[str] = None


class OrganizationAccountsResponse(BaseModel):
    accounts: List[OrganizationAccount]
