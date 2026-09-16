"""Targeted, single-resource live AWS re-fetches for drift_reconciliation_agent.

Deliberately NOT a re-run of tools/aws_scanner.py's full paginated account
scans - drift_reconciliation_agent only ever needs to re-check the handful
of resources actually being adopted (safe_to_import / use_data_source), so a
targeted Describe*(ids=[...]) call per resource is faster and cheaper than
re-scanning the whole account a second time.

Uses the exact same boto3 session-construction pattern as
tools/aws_scanner.py::AWSScanner.__init__ (same credential fields, same
LocalStack endpoint_url override for tests) - read-only Describe* APIs only,
same as every other AWS call in this codebase. Each function returns a dict
shaped identically to the matching tools/aws_scanner.py::scan_* function's
per-resource output, so drift_reconciliation_agent can diff same-named
fields directly. Returns None if the resource no longer exists (deleted
since discovery) - a real, distinct finding from an attribute mismatch.
"""

import logging
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger("terraagent.aws_live_fetch")


def _new_retry_config() -> Config:
    # Same reasoning as aws_scanner.py::_new_retry_config - a fresh Config
    # per client() call, since botocore mutates one in place on first use.
    return Config(retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=5, read_timeout=15)


def _get_tag(tags: List[Dict[str, str]], key: str, default: str) -> str:
    for t in tags:
        if t.get("Key") == key:
            return t.get("Value", default)
    return default


def build_session(aws_credentials: Dict[str, Optional[str]], region: str) -> boto3.Session:
    return boto3.Session(
        aws_access_key_id=aws_credentials.get("access_key"),
        aws_secret_access_key=aws_credentials.get("secret_key"),
        aws_session_token=aws_credentials.get("session_token"),
        region_name=region,
    )


def fetch_live_vpc(session: boto3.Session, vpc_id: str, endpoint_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        ec2 = session.client("ec2", config=_new_retry_config(), endpoint_url=endpoint_url)
        resp = ec2.describe_vpcs(VpcIds=[vpc_id])
        vpcs = resp.get("Vpcs", [])
        if not vpcs:
            return None
        vpc = vpcs[0]
        return {
            "resource_type": "aws_vpc",
            "id": vpc_id,
            "name": _get_tag(vpc.get("Tags", []), "Name", vpc_id),
            "cidr_block": vpc.get("CidrBlock"),
            "is_default": vpc.get("IsDefault", False),
            "tags": vpc.get("Tags", []),
        }
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("InvalidVpcID.NotFound",):
            return None
        logger.warning(f"Error re-fetching live VPC {vpc_id}: {e.response['Error']['Message']}")
        return None


def fetch_live_subnet(session: boto3.Session, subnet_id: str, endpoint_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        ec2 = session.client("ec2", config=_new_retry_config(), endpoint_url=endpoint_url)
        resp = ec2.describe_subnets(SubnetIds=[subnet_id])
        subnets = resp.get("Subnets", [])
        if not subnets:
            return None
        s = subnets[0]
        return {
            "resource_type": "aws_subnet",
            "id": subnet_id,
            "name": _get_tag(s.get("Tags", []), "Name", subnet_id),
            "vpc_id": s.get("VpcId"),
            "cidr_block": s.get("CidrBlock"),
            "availability_zone": s.get("AvailabilityZone"),
            "tags": s.get("Tags", []),
        }
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("InvalidSubnetID.NotFound",):
            return None
        logger.warning(f"Error re-fetching live subnet {subnet_id}: {e.response['Error']['Message']}")
        return None


def fetch_live_security_group(session: boto3.Session, sg_id: str, endpoint_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        ec2 = session.client("ec2", config=_new_retry_config(), endpoint_url=endpoint_url)
        resp = ec2.describe_security_groups(GroupIds=[sg_id])
        groups = resp.get("SecurityGroups", [])
        if not groups:
            return None
        sg = groups[0]
        return {
            "resource_type": "aws_security_group",
            "id": sg_id,
            "name": sg.get("GroupName", sg_id),
            "vpc_id": sg.get("VpcId"),
            "description": sg.get("Description"),
            "ip_permissions": sg.get("IpPermissions", []),
            "ip_permissions_egress": sg.get("IpPermissionsEgress", []),
            "tags": sg.get("Tags", []),
        }
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("InvalidGroup.NotFound",):
            return None
        logger.warning(f"Error re-fetching live security group {sg_id}: {e.response['Error']['Message']}")
        return None


def fetch_live_instance(session: boto3.Session, instance_id: str, endpoint_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        ec2 = session.client("ec2", config=_new_retry_config(), endpoint_url=endpoint_url)
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        for reservation in resp.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                if inst.get("State", {}).get("Name") == "terminated":
                    return None
                return {
                    "resource_type": "aws_instance",
                    "id": instance_id,
                    "name": _get_tag(inst.get("Tags", []), "Name", instance_id),
                    "instance_type": inst.get("InstanceType"),
                    "subnet_id": inst.get("SubnetId"),
                    "vpc_id": inst.get("VpcId"),
                    "security_groups": [g["GroupId"] for g in inst.get("SecurityGroups", [])],
                    "iam_instance_profile_arn": inst.get("IamInstanceProfile", {}).get("Arn"),
                    "tags": inst.get("Tags", []),
                }
        return None
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("InvalidInstanceID.NotFound",):
            return None
        logger.warning(f"Error re-fetching live instance {instance_id}: {e.response['Error']['Message']}")
        return None


def fetch_live_route_table(session: boto3.Session, rt_id: str, endpoint_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        ec2 = session.client("ec2", config=_new_retry_config(), endpoint_url=endpoint_url)
        resp = ec2.describe_route_tables(RouteTableIds=[rt_id])
        tables = resp.get("RouteTables", [])
        if not tables:
            return None
        rt = tables[0]
        associated_subnets = [a.get("SubnetId") for a in rt.get("Associations", []) if a.get("SubnetId")]
        routes = [
            {
                "destination_cidr_block": r.get("DestinationCidrBlock"),
                "gateway_id": r.get("GatewayId"),
                "nat_gateway_id": r.get("NatGatewayId"),
                "state": r.get("State"),
            }
            for r in rt.get("Routes", [])
        ]
        return {
            "resource_type": "aws_route_table",
            "id": rt_id,
            "name": _get_tag(rt.get("Tags", []), "Name", rt_id),
            "vpc_id": rt.get("VpcId"),
            "routes": routes,
            "associated_subnets": associated_subnets,
            "tags": rt.get("Tags", []),
        }
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("InvalidRouteTableID.NotFound",):
            return None
        logger.warning(f"Error re-fetching live route table {rt_id}: {e.response['Error']['Message']}")
        return None


def fetch_live_s3_bucket(session: boto3.Session, bucket_name: str, endpoint_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        s3 = session.client("s3", config=_new_retry_config(), endpoint_url=endpoint_url)
        s3.head_bucket(Bucket=bucket_name)
        return {
            "resource_type": "aws_s3_bucket",
            "id": bucket_name,
            "name": bucket_name,
        }
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchBucket"):
            return None
        logger.warning(f"Error re-fetching live S3 bucket {bucket_name}: {e.response['Error']['Message']}")
        return None


_FETCHERS = {
    "aws_vpc": fetch_live_vpc,
    "aws_subnet": fetch_live_subnet,
    "aws_security_group": fetch_live_security_group,
    "aws_instance": fetch_live_instance,
    "aws_route_table": fetch_live_route_table,
    "aws_s3_bucket": fetch_live_s3_bucket,
}

# Public - drift_reconciliation_agent uses this to decide which adopted
# resources it can re-check at all (deterministic-template types only, same
# scope as terraform_composer.py's own templates; anything composed via the
# LLM fallback path isn't covered in this version).
SUPPORTED_RESOURCE_TYPES = frozenset(_FETCHERS.keys())


def fetch_live_resource(
    session: boto3.Session, resource_type: str, resource_id: str, endpoint_url: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Dispatches to the matching fetch_live_* function, or returns None for
    a resource_type this module doesn't know how to re-fetch (the same
    deterministic-template-types-only scope terraform_composer.py's own
    templates cover - anything else falls back to the LLM composition path,
    which drift_reconciliation_agent doesn't check in this version)."""
    fetcher = _FETCHERS.get(resource_type)
    if fetcher is None:
        return None
    return fetcher(session, resource_id, endpoint_url)
