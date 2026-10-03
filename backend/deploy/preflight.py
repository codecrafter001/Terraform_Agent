"""Pre-plan checks against the target account (read-only: Describe*/Get* only).

Container targets create one VPC per deployment. AWS allows 5 VPCs per region by
default, so the sixth deployment would fail half-way through apply and leave a
partial stack behind. Checking before the plan turns that into a clear message
before anything is created.
"""

import logging
from typing import Dict, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger("terraagent.deploy.preflight")

VPC_TARGETS = frozenset({"ecs_service", "fullstack_app"})
VPC_QUOTA_CODE = "L-F678F1CE"  # Amazon VPC: "VPCs per Region"
DEFAULT_VPC_QUOTA = 5
_CONFIG = Config(retries={"max_attempts": 3, "mode": "adaptive"}, connect_timeout=5, read_timeout=15)


def _session(creds: Dict[str, str], region: str) -> boto3.session.Session:
    return boto3.session.Session(
        aws_access_key_id=creds.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=creds.get("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=creds.get("AWS_SESSION_TOKEN"),
        region_name=region,
    )


def vpc_quota_problem(creds: Dict[str, str], region: str) -> Optional[str]:
    """A user-facing message when creating one more VPC in `region` would exceed
    the account's quota; None when there's room or the check can't run (a failed
    check never blocks a plan - Terraform would report the real error)."""
    session = _session(creds, region)
    try:
        ec2 = session.client("ec2", config=_CONFIG)
        count = 0
        for page in ec2.get_paginator("describe_vpcs").paginate():
            count += len(page.get("Vpcs", []))
    except (ClientError, BotoCoreError) as e:
        logger.warning(f"VPC pre-check skipped: DescribeVpcs failed in {region}: {type(e).__name__}")
        return None
    quota = DEFAULT_VPC_QUOTA
    try:
        sq = session.client("service-quotas", config=_CONFIG)
        quota = int(sq.get_service_quota(ServiceCode="vpc", QuotaCode=VPC_QUOTA_CODE)["Quota"]["Value"])
    except (ClientError, BotoCoreError, KeyError, ValueError):
        pass  # no servicequotas permission or no applied value: AWS's default
    if count < quota:
        return None
    return (
        f"The account already has {count} VPCs in {region}, and its limit is {quota}. This deployment needs its own "
        f"VPC, so it would fail part-way through. Delete an unused VPC, or request a higher 'VPCs per Region' quota "
        f"(Service Quotas -> Amazon VPC -> {VPC_QUOTA_CODE}), then run the plan again. Nothing was created."
    )
