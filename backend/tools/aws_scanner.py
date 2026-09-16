"""AWS Read-Only Cloud Discovery Scanner.
Enforces strictly read-only Describe*, Get*, List* APIs.
Never logs credentials or uses mutating calls.
"""

import logging
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from tools.cloud_discovery_interface import CloudDiscoveryInterface

logger = logging.getLogger("terraagent.aws_scanner")


def _new_retry_config() -> Config:
    # A fresh Config instance per client() call, deliberately - botocore
    # mutates a Config object in place the first time it's used to build a
    # client (normalizing "max_attempts" into an internal "total_max_attempts"
    # key), so sharing one Config singleton across the 8 client() calls in
    # this file would silently change retry behavior after the first call.
    return Config(
        retries={"max_attempts": 5, "mode": "standard"},
        connect_timeout=5,
        read_timeout=15
    )


# Kept for readability at call sites / introspection in tests. Never pass
# this exact object into boto3.client() - use _new_retry_config() instead.
RETRY_CONFIG = _new_retry_config()


class AWSScanner(CloudDiscoveryInterface):
    def __init__(
        self,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        session_token: Optional[str] = None,
        endpoint_url: Optional[str] = None
    ):
        # endpoint_url overrides the real AWS endpoint - used only to point at
        # LocalStack in integration tests; production calls leave this unset
        # and hit real AWS.
        self.region = region
        self.endpoint_url = endpoint_url
        self.session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
            region_name=region
        )

    def scan_vpcs(self) -> List[Dict[str, Any]]:
        """Scan EC2 VPCs, subnets, and internet gateways."""
        results = []
        try:
            ec2 = self.session.client("ec2", config=_new_retry_config(), endpoint_url=self.endpoint_url)
            for page in ec2.get_paginator("describe_vpcs").paginate():
                for vpc in page.get("Vpcs", []):
                    vpc_id = vpc["VpcId"]
                    results.append({
                        "resource_type": "aws_vpc",
                        "id": vpc_id,
                        "name": self._get_tag(vpc.get("Tags", []), "Name", vpc_id),
                        "cidr_block": vpc.get("CidrBlock"),
                        "is_default": vpc.get("IsDefault", False),
                        "tags": vpc.get("Tags", [])
                    })
        except ClientError as e:
            logger.warning(f"Error scanning VPCs: {e.response['Error']['Message']}")
        return results

    def scan_subnets(self) -> List[Dict[str, Any]]:
        """Scan Subnets."""
        results = []
        try:
            ec2 = self.session.client("ec2", config=_new_retry_config(), endpoint_url=self.endpoint_url)
            for page in ec2.get_paginator("describe_subnets").paginate():
                for s in page.get("Subnets", []):
                    sub_id = s["SubnetId"]
                    results.append({
                        "resource_type": "aws_subnet",
                        "id": sub_id,
                        "name": self._get_tag(s.get("Tags", []), "Name", sub_id),
                        "vpc_id": s.get("VpcId"),
                        "cidr_block": s.get("CidrBlock"),
                        "availability_zone": s.get("AvailabilityZone"),
                        "tags": s.get("Tags", [])
                    })
        except ClientError as e:
            logger.warning(f"Error scanning Subnets: {e.response['Error']['Message']}")
        return results

    def scan_route_tables(self) -> List[Dict[str, Any]]:
        """Scan VPC Route Tables and their routes."""
        results = []
        try:
            ec2 = self.session.client("ec2", config=_new_retry_config(), endpoint_url=self.endpoint_url)
            for page in ec2.get_paginator("describe_route_tables").paginate():
                for rt in page.get("RouteTables", []):
                    rt_id = rt["RouteTableId"]
                    associated_subnets = [
                        a.get("SubnetId") for a in rt.get("Associations", []) if a.get("SubnetId")
                    ]
                    routes = [
                        {
                            "destination_cidr_block": r.get("DestinationCidrBlock"),
                            "gateway_id": r.get("GatewayId"),
                            "nat_gateway_id": r.get("NatGatewayId"),
                            "state": r.get("State"),
                        }
                        for r in rt.get("Routes", [])
                    ]
                    results.append({
                        "resource_type": "aws_route_table",
                        "id": rt_id,
                        "name": self._get_tag(rt.get("Tags", []), "Name", rt_id),
                        "vpc_id": rt.get("VpcId"),
                        "routes": routes,
                        "associated_subnets": associated_subnets,
                        "tags": rt.get("Tags", [])
                    })
        except ClientError as e:
            logger.warning(f"Error scanning Route Tables: {e.response['Error']['Message']}")
        return results

    def scan_security_groups(self) -> List[Dict[str, Any]]:
        """Scan EC2 Security Groups."""
        results = []
        try:
            ec2 = self.session.client("ec2", config=_new_retry_config(), endpoint_url=self.endpoint_url)
            for page in ec2.get_paginator("describe_security_groups").paginate():
                for sg in page.get("SecurityGroups", []):
                    sg_id = sg["GroupId"]
                    results.append({
                        "resource_type": "aws_security_group",
                        "id": sg_id,
                        "name": sg.get("GroupName", sg_id),
                        "vpc_id": sg.get("VpcId"),
                        "description": sg.get("Description"),
                        "ip_permissions": sg.get("IpPermissions", []),
                        "ip_permissions_egress": sg.get("IpPermissionsEgress", []),
                        "tags": sg.get("Tags", [])
                    })
        except ClientError as e:
            logger.warning(f"Error scanning Security Groups: {e.response['Error']['Message']}")
        return results

    def scan_ec2_instances(self) -> List[Dict[str, Any]]:
        """Scan EC2 Instances."""
        results = []
        try:
            ec2 = self.session.client("ec2", config=_new_retry_config(), endpoint_url=self.endpoint_url)
            for page in ec2.get_paginator("describe_instances").paginate():
                for res in page.get("Reservations", []):
                    for inst in res.get("Instances", []):
                        if inst.get("State", {}).get("Name") == "terminated":
                            continue
                        inst_id = inst["InstanceId"]
                        results.append({
                            "resource_type": "aws_instance",
                            "id": inst_id,
                            "name": self._get_tag(inst.get("Tags", []), "Name", inst_id),
                            "instance_type": inst.get("InstanceType"),
                            "subnet_id": inst.get("SubnetId"),
                            "vpc_id": inst.get("VpcId"),
                            "security_groups": [g["GroupId"] for g in inst.get("SecurityGroups", [])],
                            # Names the instance PROFILE, not the role itself - AWS decouples the
                            # two, though the console almost always names them identically. Used
                            # by graph_builder.py as a naming-convention heuristic, not a resolved
                            # reference - hence the < 1.0 confidence score on the edge it produces.
                            "iam_instance_profile_arn": inst.get("IamInstanceProfile", {}).get("Arn"),
                            "tags": inst.get("Tags", [])
                        })
        except ClientError as e:
            logger.warning(f"Error scanning EC2 instances: {e.response['Error']['Message']}")
        return results

    def scan_s3_buckets(self) -> List[Dict[str, Any]]:
        """Scan S3 Buckets in region. (list_buckets is not a paginated API.)"""
        results = []
        try:
            s3 = self.session.client("s3", config=_new_retry_config(), endpoint_url=self.endpoint_url)
            buckets = s3.list_buckets().get("Buckets", [])
            for b in buckets:
                b_name = b["Name"]
                try:
                    loc = s3.get_bucket_location(Bucket=b_name).get("LocationConstraint")
                    bucket_region = loc if loc else "us-east-1"
                    if bucket_region == "EU":
                        bucket_region = "eu-west-1"
                    if bucket_region != self.region and self.region != "all":
                        continue
                except ClientError:
                    pass

                results.append({
                    "resource_type": "aws_s3_bucket",
                    "id": b_name,
                    "name": b_name,
                    "creation_date": b.get("CreationDate", "").isoformat() if b.get("CreationDate") else None
                })
        except ClientError as e:
            logger.warning(f"Error scanning S3: {e.response['Error']['Message']}")
        return results

    def scan_rds_instances(self) -> List[Dict[str, Any]]:
        """Scan RDS DB Instances."""
        results = []
        try:
            rds = self.session.client("rds", config=_new_retry_config(), endpoint_url=self.endpoint_url)
            for page in rds.get_paginator("describe_db_instances").paginate():
                for db in page.get("DBInstances", []):
                    db_id = db["DBInstanceIdentifier"]
                    results.append({
                        "resource_type": "aws_db_instance",
                        "id": db_id,
                        "name": db_id,
                        "engine": db.get("Engine"),
                        "engine_version": db.get("EngineVersion"),
                        "instance_class": db.get("DBInstanceClass"),
                        "allocated_storage": db.get("AllocatedStorage"),
                        "multi_az": db.get("MultiAZ", False),
                        # Without these, an RDS instance has no field graph_builder.py
                        # can draw an edge from, so it always looks like a zero-degree
                        # ("orphaned") node to resource_classifier.py regardless of
                        # actual usage - the API returns both, so there's no reason
                        # to leave them out.
                        "vpc_id": db.get("DBSubnetGroup", {}).get("VpcId"),
                        "security_groups": [
                            g["VpcSecurityGroupId"] for g in db.get("VpcSecurityGroups", [])
                        ],
                        "tags": db.get("TagList", [])
                    })
        except ClientError as e:
            logger.warning(f"Error scanning RDS: {e.response['Error']['Message']}")
        return results

    def scan_iam_roles(self) -> List[Dict[str, Any]]:
        """Scan IAM Roles and their attached managed policies. IAM is a global (non-regional) service."""
        results = []
        try:
            iam = self.session.client("iam", config=_new_retry_config(), endpoint_url=self.endpoint_url)
            for page in iam.get_paginator("list_roles").paginate():
                for role in page.get("Roles", []):
                    role_name = role["RoleName"]
                    attached_policies: List[str] = []
                    try:
                        for pol_page in iam.get_paginator("list_attached_role_policies").paginate(RoleName=role_name):
                            attached_policies.extend(
                                p.get("PolicyArn") for p in pol_page.get("AttachedPolicies", [])
                            )
                    except ClientError as e:
                        logger.warning(f"Error listing policies for role {role_name}: {e.response['Error']['Message']}")

                    results.append({
                        "resource_type": "aws_iam_role",
                        "id": role_name,
                        "name": role_name,
                        "arn": role.get("Arn"),
                        "assume_role_policy": role.get("AssumeRolePolicyDocument"),
                        "attached_policy_arns": attached_policies,
                        "tags": role.get("Tags", [])
                    })
        except ClientError as e:
            logger.warning(f"Error scanning IAM roles: {e.response['Error']['Message']}")
        return results

    def scan_all(self, filters: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Run all resource scans according to enabled category filters."""
        all_resources: List[Dict[str, Any]] = []
        f = [x.upper() for x in (filters or ["VPC", "EC2", "S3", "RDS", "SG"])]

        if "VPC" in f:
            all_resources.extend(self.scan_vpcs())
            all_resources.extend(self.scan_subnets())
            all_resources.extend(self.scan_route_tables())
        if "SG" in f:
            all_resources.extend(self.scan_security_groups())
        if "EC2" in f:
            all_resources.extend(self.scan_ec2_instances())
        if "S3" in f:
            all_resources.extend(self.scan_s3_buckets())
        if "RDS" in f:
            all_resources.extend(self.scan_rds_instances())
        if "IAM" in f:
            all_resources.extend(self.scan_iam_roles())

        return all_resources

    @staticmethod
    def _get_tag(tags: List[Dict[str, str]], key: str, default: str) -> str:
        for t in tags:
            if t.get("Key") == key:
                return t.get("Value", default)
        return default
