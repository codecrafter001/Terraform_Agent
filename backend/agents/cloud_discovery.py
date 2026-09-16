"""Agent 2: Cloud Discovery Agent.
Queries AWS APIs with strictly read-only calls to discover live infrastructure resources.
"""

import logging
import os
from typing import Any, Dict

from botocore.exceptions import ClientError

from services.redis_client import redis_service
from tools.aws_scanner import AWSScanner
from tools.sts_helper import assume_role

logger = logging.getLogger("terraagent.agents.cloud_discovery")


async def cloud_discovery_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Scans AWS resources based on credentials and filters in state."""
    job_id = state.get("job_id", "")
    region = state.get("region", "us-east-1")
    filters = state.get("resource_filters", ["EC2", "VPC", "S3", "RDS", "SG"])

    logger.info(f"[{job_id}] Cloud Discovery: Scanning region '{region}' with filters {filters}")

    # Safe retrieval of credentials from transient state (never logged)
    aws_credentials = state.get("aws_credentials", {})
    access_key = aws_credentials.get("access_key", "")
    secret_key = aws_credentials.get("secret_key", "")
    session_token = aws_credentials.get("session_token")

    # Optional override for integration tests only (points boto3 at LocalStack
    # instead of real AWS) - state takes precedence over the env var so a
    # normal production request (which never sets this key) is unaffected.
    endpoint_url = state.get("aws_endpoint_url") or os.getenv("AWS_ENDPOINT_URL")
    role_arn = state.get("role_arn")

    discovered_resources = []
    errors = list(state.get("errors", []))
    if access_key and secret_key:
        scan_access_key, scan_secret_key, scan_session_token = access_key, secret_key, session_token

        if role_arn:
            try:
                await redis_service.publish_log(
                    job_id, f"[AGENT:cloud_discovery] Assuming role {role_arn} via STS for multi-account access...",
                    agent_name="cloud_discovery"
                )
                scan_access_key, scan_secret_key, scan_session_token = assume_role(
                    role_arn, access_key, secret_key, session_token, region,
                    session_name=f"terraagent-{job_id}", endpoint_url=endpoint_url
                )
            except ClientError as e:
                # Deliberately do NOT fall through to scanning with the
                # caller's own raw credentials here: if the user asked to
                # scan a specific target account via role_arn and that
                # assumption fails, silently scanning the caller's own
                # account instead would return a real (just wrong) resource
                # inventory - indistinguishable from a successful scan of the
                # intended account unless someone reads the error list.
                msg = f"Failed to assume role {role_arn}: {e.response['Error'].get('Message', str(e))}"
                logger.warning(f"[{job_id}] {msg}")
                errors.append(msg)
                await redis_service.publish_log(job_id, f"[AGENT:cloud_discovery] {msg}", agent_name="cloud_discovery")
                scan_access_key, scan_secret_key, scan_session_token = None, None, None

        if scan_access_key and scan_secret_key:
            scanner = AWSScanner(
                access_key=scan_access_key,
                secret_key=scan_secret_key,
                region=region,
                session_token=scan_session_token,
                endpoint_url=endpoint_url
            )
            discovered_resources = scanner.scan_all(filters=filters)
    else:
        logger.warning(f"[{job_id}] No AWS credentials provided, generating mock discovered resources for simulation.")
        discovered_resources = [
            {
                "resource_type": "aws_vpc",
                "id": "vpc-0123456789abcdef0",
                "name": "terraagent-main-vpc",
                "cidr_block": "10.0.0.0/16",
                "tags": [{"Key": "Name", "Value": "terraagent-main-vpc"}]
            },
            {
                "resource_type": "aws_subnet",
                "id": "subnet-0123456789abcdef0",
                "name": "terraagent-public-subnet-1",
                "vpc_id": "vpc-0123456789abcdef0",
                "cidr_block": "10.0.1.0/24",
                "availability_zone": f"{region}a",
                "tags": [{"Key": "Name", "Value": "terraagent-public-subnet-1"}]
            },
            {
                "resource_type": "aws_security_group",
                "id": "sg-0123456789abcdef0",
                "name": "terraagent-web-sg",
                "vpc_id": "vpc-0123456789abcdef0",
                "description": "Allow HTTP and HTTPS inbound",
                "ip_permissions": [
                    {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                    {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
                ],
                "tags": [{"Key": "Name", "Value": "terraagent-web-sg"}]
            },
            {
                "resource_type": "aws_instance",
                "id": "i-0123456789abcdef0",
                "name": "terraagent-web-server",
                "instance_type": "t3.micro",
                "subnet_id": "subnet-0123456789abcdef0",
                "vpc_id": "vpc-0123456789abcdef0",
                "security_groups": ["sg-0123456789abcdef0"],
                "tags": [{"Key": "Name", "Value": "terraagent-web-server"}]
            },
            {
                "resource_type": "aws_s3_bucket",
                "id": "terraagent-app-assets-bucket",
                "name": "terraagent-app-assets-bucket",
                "tags": [{"Key": "Environment", "Value": "Production"}]
            },
            {
                "resource_type": "aws_route_table",
                "id": "rtb-0123456789abcdef0",
                "name": "terraagent-public-rt",
                "vpc_id": "vpc-0123456789abcdef0",
                "routes": [
                    {"destination_cidr_block": "0.0.0.0/0", "gateway_id": "igw-0123456789abcdef0", "state": "active"}
                ],
                "associated_subnets": ["subnet-0123456789abcdef0"],
                "tags": [{"Key": "Name", "Value": "terraagent-public-rt"}]
            },
            {
                "resource_type": "aws_iam_role",
                "id": "terraagent-ec2-role",
                "name": "terraagent-ec2-role",
                "arn": "arn:aws:iam::123456789012:role/terraagent-ec2-role",
                "attached_policy_arns": ["arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"],
                "tags": [{"Key": "Name", "Value": "terraagent-ec2-role"}]
            }
        ]

    completed_agents = list(state.get("completed_agents", []))
    completed_agents.append("cloud_discovery")

    return {
        "resources": discovered_resources,
        "completed_agents": completed_agents,
        "current_agent": "graph_agent",
        "progress_percentage": 30,
        "errors": errors
    }
