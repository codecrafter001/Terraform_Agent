"""AWS Read-Only Cloud Discovery Scanner.
Enforces strictly read-only Describe*, Get*, List* APIs.
Never logs credentials or uses mutating calls.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from tools.aws_scanner_services import ExtraServiceScans
from tools.cloud_discovery_interface import CloudDiscoveryInterface

logger = logging.getLogger("terraagent.aws_scanner")


def _new_retry_config() -> Config:
    # A fresh Config instance per client() call, deliberately - botocore
    # mutates a Config object in place the first time it's used to build a
    # client (normalizing "max_attempts" into an internal "total_max_attempts"
    # key), so sharing one Config singleton across the 8 client() calls in
    # this file would silently change retry behavior after the first call.
    # "adaptive" = standard retries plus client-side rate limiting, so a
    # throttled API slows this scanner down instead of burning its retries.
    return Config(
        retries={"max_attempts": 8, "mode": "adaptive"},
        connect_timeout=5,
        read_timeout=15
    )


# Parallel service scans - each with its own client.
SCAN_WORKERS = 8


# Kept for readability at call sites / introspection in tests. Never pass
# this exact object into boto3.client() - use _new_retry_config() instead.
RETRY_CONFIG = _new_retry_config()


class AWSScanner(ExtraServiceScans, CloudDiscoveryInterface):
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
        # A boto3 Session isn't safe for creating clients from several threads
        # at once; the clients themselves are.
        self._client_lock = threading.Lock()
        # Every call that failed after retries (throttling, access denied,
        # network). A scan with any of these is INCOMPLETE - a failed call must
        # never look like an empty account.
        self.errors: List[Dict[str, str]] = []
        self._errors_lock = threading.Lock()
        self.counts: Dict[str, int] = {}

    def _client(self, service: str) -> Any:
        with self._client_lock:
            return self.session.client(service, config=_new_retry_config(), endpoint_url=self.endpoint_url)

    def _failed(self, scope: str, error: Exception) -> None:
        if isinstance(error, ClientError):
            code = error.response.get("Error", {}).get("Code", "ClientError")
            message = error.response.get("Error", {}).get("Message", "")
        else:
            code, message = type(error).__name__, str(error)
        logger.warning(f"Error scanning {scope}: {code} {message}")
        with self._errors_lock:
            self.errors.append({"scope": scope, "code": code, "message": message[:300]})

    def scan_vpcs(self) -> List[Dict[str, Any]]:
        """Scan EC2 VPCs, subnets, and internet gateways."""
        results = []
        try:
            ec2 = self._client("ec2")
            for page in ec2.get_paginator("describe_vpcs").paginate():
                for vpc in page.get("Vpcs", []):
                    vpc_id = vpc["VpcId"]
                    record = {
                        "resource_type": "aws_vpc",
                        "id": vpc_id,
                        "name": self._get_tag(vpc.get("Tags", []), "Name", vpc_id),
                        "cidr_block": vpc.get("CidrBlock"),
                        "is_default": vpc.get("IsDefault", False),
                        "tags": vpc.get("Tags", [])
                    }
                    # Rendered as-is in the adoption code; unknown -> omitted there.
                    for attribute, field, key in (
                        ("enableDnsSupport", "enable_dns_support", "EnableDnsSupport"),
                        ("enableDnsHostnames", "enable_dns_hostnames", "EnableDnsHostnames"),
                    ):
                        try:
                            value = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute=attribute)
                            record[field] = bool(value.get(key, {}).get("Value"))
                        except (ClientError, BotoCoreError) as e:
                            self._failed(f"{attribute} of {vpc_id}", e)
                    results.append(record)
        except (ClientError, BotoCoreError) as e:
            self._failed("VPCs", e)
        return results

    def scan_subnets(self) -> List[Dict[str, Any]]:
        """Scan Subnets."""
        results = []
        try:
            ec2 = self._client("ec2")
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
                        "map_public_ip_on_launch": s.get("MapPublicIpOnLaunch"),
                        "tags": s.get("Tags", [])
                    })
        except (ClientError, BotoCoreError) as e:
            self._failed("Subnets", e)
        return results

    def scan_route_tables(self) -> List[Dict[str, Any]]:
        """Scan VPC Route Tables and their routes."""
        results = []
        try:
            ec2 = self._client("ec2")
            for page in ec2.get_paginator("describe_route_tables").paginate():
                for rt in page.get("RouteTables", []):
                    rt_id = rt["RouteTableId"]
                    associated_subnets = [
                        a.get("SubnetId") for a in rt.get("Associations", []) if a.get("SubnetId")
                    ]
                    # Every destination and target kind, so the adoption code can
                    # render each route exactly (tools/hcl_render.py::route_lines).
                    routes = [
                        {
                            "destination_cidr_block": r.get("DestinationCidrBlock"),
                            "destination_ipv6_cidr_block": r.get("DestinationIpv6CidrBlock"),
                            "destination_prefix_list_id": r.get("DestinationPrefixListId"),
                            "gateway_id": r.get("GatewayId"),
                            "nat_gateway_id": r.get("NatGatewayId"),
                            "transit_gateway_id": r.get("TransitGatewayId"),
                            "vpc_peering_connection_id": r.get("VpcPeeringConnectionId"),
                            "egress_only_internet_gateway_id": r.get("EgressOnlyInternetGatewayId"),
                            "carrier_gateway_id": r.get("CarrierGatewayId"),
                            "local_gateway_id": r.get("LocalGatewayId"),
                            "core_network_arn": r.get("CoreNetworkArn"),
                            "network_interface_id": r.get("NetworkInterfaceId"),
                            "instance_id": r.get("InstanceId"),
                            "origin": r.get("Origin"),
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
        except (ClientError, BotoCoreError) as e:
            self._failed("Route Tables", e)
        return results

    def scan_security_groups(self) -> List[Dict[str, Any]]:
        """Scan EC2 Security Groups."""
        results = []
        try:
            ec2 = self._client("ec2")
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
        except (ClientError, BotoCoreError) as e:
            self._failed("Security Groups", e)
        return results

    def scan_ec2_instances(self) -> List[Dict[str, Any]]:
        """Scan EC2 Instances."""
        results = []
        try:
            ec2 = self._client("ec2")
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
                            "ami": inst.get("ImageId"),
                            # Read for the Hardening proposal (IMDSv2); never written into adoption code.
                            "metadata_http_tokens": inst.get("MetadataOptions", {}).get("HttpTokens"),
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
        except (ClientError, BotoCoreError) as e:
            self._failed("EC2 instances", e)
        return results

    def scan_s3_buckets(self) -> List[Dict[str, Any]]:
        """Scan S3 Buckets in region. (list_buckets is not a paginated API.)"""
        results = []
        try:
            s3 = self._client("s3")
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
                    "creation_date": b.get("CreationDate", "").isoformat() if b.get("CreationDate") else None,
                    **self._s3_bucket_details(s3, b_name),
                })
        except (ClientError, BotoCoreError) as e:
            self._failed("S3", e)
        return results

    def _s3_bucket_details(self, s3: Any, bucket: str) -> Dict[str, Any]:
        """Tags (rendered exactly in the adoption code) plus the public access
        block and default encryption (read for the Hardening proposal only).
        Read-only: GetBucketTagging, GetPublicAccessBlock, GetBucketEncryption.
        A missing configuration is recorded as absent; an unreadable one as
        None (unknown), never guessed."""
        details: Dict[str, Any] = {}
        calls = (
            ("tags", lambda: s3.get_bucket_tagging(Bucket=bucket).get("TagSet", []), ("NoSuchTagSet",), []),
            ("public_access_block", lambda: s3.get_public_access_block(Bucket=bucket)
             .get("PublicAccessBlockConfiguration", {}), ("NoSuchPublicAccessBlockConfiguration",), {}),
            ("encryption_rules", lambda: s3.get_bucket_encryption(Bucket=bucket)
             .get("ServerSideEncryptionConfiguration", {}).get("Rules", []),
             ("ServerSideEncryptionConfigurationNotFoundError",), []),
        )
        for field, call, absent_codes, absent_value in calls:
            try:
                details[field] = call()
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                details[field] = absent_value if code in absent_codes else None
                if code not in absent_codes:
                    self._failed(f"{field} of bucket {bucket}", e)
            except BotoCoreError as e:
                details[field] = None
                self._failed(f"{field} of bucket {bucket}", e)
        if details.get("tags") is None:
            details.pop("tags")
        return details

    def scan_rds_instances(self) -> List[Dict[str, Any]]:
        """Scan RDS DB Instances."""
        results = []
        try:
            rds = self._client("rds")
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
        except (ClientError, BotoCoreError) as e:
            self._failed("RDS", e)
        return results

    def scan_iam_roles(self) -> List[Dict[str, Any]]:
        """Scan IAM Roles and their attached managed policies. IAM is a global (non-regional) service."""
        results = []
        try:
            iam = self._client("iam")
            for page in iam.get_paginator("list_roles").paginate():
                for role in page.get("Roles", []):
                    role_name = role["RoleName"]
                    attached_policies: List[str] = []
                    try:
                        for pol_page in iam.get_paginator("list_attached_role_policies").paginate(RoleName=role_name):
                            attached_policies.extend(
                                p.get("PolicyArn") for p in pol_page.get("AttachedPolicies", [])
                            )
                    except (ClientError, BotoCoreError) as e:
                        self._failed(f"attached policies of role {role_name}", e)

                    results.append({
                        "resource_type": "aws_iam_role",
                        "id": role_name,
                        "name": role_name,
                        "arn": role.get("Arn"),
                        "path": role.get("Path"),
                        "description": role.get("Description"),
                        "max_session_duration": role.get("MaxSessionDuration"),
                        "assume_role_policy": role.get("AssumeRolePolicyDocument"),
                        "attached_policy_arns": attached_policies,
                        "tags": role.get("Tags", [])
                    })
        except (ClientError, BotoCoreError) as e:
            self._failed("IAM roles", e)
        return results

    def scan_all(self, filters: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Run the enabled service scans in parallel. Results keep a fixed
        order (same as a sequential scan); self.counts has one entry per scan
        and self.errors every call that failed after retries."""
        f = [x.upper() for x in (filters or ["VPC", "EC2", "S3", "RDS", "SG", "ELB", "DYNAMODB", "KMS", "SQS", "SNS"])]
        plan: List[Tuple[str, Callable[[], List[Dict[str, Any]]]]] = []
        if "VPC" in f:
            plan += [
                ("vpcs", self.scan_vpcs),
                ("subnets", self.scan_subnets),
                ("route_tables", self.scan_route_tables),
                ("internet_gateways", self.scan_internet_gateways),
                ("nat_gateways", self.scan_nat_gateways),
            ]
        if "SG" in f:
            plan.append(("security_groups", self.scan_security_groups))
        if "EC2" in f:
            plan.append(("ec2_instances", self.scan_ec2_instances))
        if "ELB" in f:
            plan.append(("load_balancers", self.scan_load_balancers))
        if "S3" in f:
            plan.append(("s3_buckets", self.scan_s3_buckets))
        if "RDS" in f:
            plan.append(("rds_instances", self.scan_rds_instances))
        if "DYNAMODB" in f:
            plan.append(("dynamodb_tables", self.scan_dynamodb_tables))
        if "KMS" in f:
            plan.append(("kms_keys", self.scan_kms_keys))
        if "SQS" in f:
            plan.append(("sqs_queues", self.scan_sqs_queues))
        if "SNS" in f:
            plan.append(("sns_topics", self.scan_sns_topics))
        if "IAM" in f:
            plan.append(("iam_roles", self.scan_iam_roles))

        def run(fn: Callable[[], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
            try:
                return fn()
            except Exception as e:  # never let one service sink the others
                self._failed(fn.__name__, e)
                return []

        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            results = list(pool.map(run, [fn for _, fn in plan]))
        all_resources: List[Dict[str, Any]] = []
        for (name, _), found in zip(plan, results):
            self.counts[name] = len(found)
            all_resources.extend(found)
        return all_resources

    def report(self) -> Dict[str, Any]:
        """Per-service counts and failures for this scan."""
        return {"region": self.region, "complete": not self.errors, "counts": dict(self.counts),
                "errors": list(self.errors)}

    @staticmethod
    def _get_tag(tags: List[Dict[str, str]], key: str, default: str) -> str:
        for t in tags:
            if t.get("Key") == key:
                return t.get("Value", default)
        return default
