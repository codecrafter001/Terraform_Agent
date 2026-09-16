"""AWS Organizations read-only account enumeration.

Scoped deliberately narrow: lists member accounts of an Organization via a
management-account role (organizations:ListAccounts, a read-only API), so an
operator can see which accounts exist and kick off one scan per account_id
using the existing per-scan role_arn (STS AssumeRole) mechanism in
cloud_discovery_node. It intentionally does NOT attempt to fan a single scan
job out across every member account automatically - that would need the
LangGraph pipeline itself to become multi-account-aware (looping discovery,
composition, and validation per account and merging N result sets), which is
a real architecture change and untestable here without a real AWS
Organization to verify against. This module is the real, working building
block for that: enumerate accounts, then scan each one explicitly.
"""

from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from tools.sts_helper import assume_role


def list_organization_accounts(
    access_key: str,
    secret_key: str,
    session_token: Optional[str] = None,
    management_role_arn: Optional[str] = None,
    region: str = "us-east-1",
    endpoint_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Returns [{account_id, name, email, status}, ...] for every account in
    the Organization. organizations:ListAccounts must be called from the
    management account (or a role delegated admin access to it) - pass
    management_role_arn to assume into that role first if the caller's own
    credentials aren't already in the management account."""
    scan_access_key, scan_secret_key, scan_session_token = access_key, secret_key, session_token

    if management_role_arn:
        scan_access_key, scan_secret_key, scan_session_token = assume_role(
            management_role_arn, access_key, secret_key, session_token, region,
            session_name="terraagent-org-lookup", endpoint_url=endpoint_url
        )

    session = boto3.Session(
        aws_access_key_id=scan_access_key,
        aws_secret_access_key=scan_secret_key,
        aws_session_token=scan_session_token,
        region_name=region
    )
    # Organizations is a global service - always us-east-1 regardless of `region`.
    org = session.client("organizations", region_name="us-east-1", endpoint_url=endpoint_url)

    accounts: List[Dict[str, Any]] = []
    try:
        for page in org.get_paginator("list_accounts").paginate():
            for acct in page.get("Accounts", []):
                accounts.append({
                    "account_id": acct.get("Id"),
                    "name": acct.get("Name"),
                    "email": acct.get("Email"),
                    "status": acct.get("Status")
                })
    except ClientError as e:
        raise RuntimeError(
            f"Failed to list Organization accounts: {e.response['Error'].get('Message', str(e))}"
        ) from e

    return accounts
