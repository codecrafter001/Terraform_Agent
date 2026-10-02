"""STS session helpers for deployment mode (design doc §7.3).

Generates short-lived, least-privilege credentials scoped to a single deployment.
Credentials exist only in local memory variables passed to subprocess environments,
never written to disk, databases, logs or message brokers.
"""

import json
from typing import Any, Dict, Optional

from tools.sts_helper import assume_role


def plan_session(
    target: Dict[str, Any],
    deployment_id: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    session_token: Optional[str] = None,
) -> Dict[str, str]:
    """Assumes the target's plan role for 15 minutes with a read-only session policy."""
    target_id = target.get("id", "default")
    state_bucket = target.get("state_bucket", "")
    role_arn = target.get("plan_role_arn", "")
    external_id = target.get("external_id")
    region = target.get("region", "us-east-1")

    # Scoped session policy: read-only access + S3 state file read/lock access
    session_policy = jsonencode_policy({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowStateAccess",
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:ListBucket",
                    "s3:PutObject",
                    "s3:DeleteObject"
                ],
                "Resource": [
                    f"arn:aws:s3:::{state_bucket}",
                    f"arn:aws:s3:::{state_bucket}/terraagent/{target_id}/{deployment_id}.tfstate*"
                ]
            },
            {
                "Sid": "AllowDescribeResources",
                "Effect": "Allow",
                "Action": [
                    "s3:Get*",
                    "s3:List*",
                    "lambda:Get*",
                    "lambda:List*",
                    "apigateway:GET",
                    "cloudfront:Get*",
                    "cloudfront:List*",
                    "logs:Describe*",
                    "logs:Get*",
                    "iam:Get*",
                    "iam:List*"
                ],
                "Resource": "*"
            }
        ]
    })

    ak, sk, st = assume_role(
        role_arn=role_arn,
        access_key=access_key,
        secret_key=secret_key,
        session_token=session_token,
        region=region,
        session_name=f"terraagent-plan-{deployment_id}"[:64],
        external_id=external_id,
        source_identity="terraagent-system",
        tags=[{"Key": "deployment_id", "Value": deployment_id}],
        policy=session_policy,
        duration_seconds=900,
    )

    return {
        "AWS_ACCESS_KEY_ID": ak,
        "AWS_SECRET_ACCESS_KEY": sk,
        "AWS_SESSION_TOKEN": st,
        "AWS_DEFAULT_REGION": region,
        "AWS_REGION": region,
    }


def apply_session(
    target: Dict[str, Any],
    deployment_id: str,
    approver_email: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    session_token: Optional[str] = None,
) -> Dict[str, str]:
    """Assumes the target's apply role for 1 hour with SourceIdentity bound to approver."""
    role_arn = target.get("apply_role_arn", "")
    external_id = target.get("external_id")
    region = target.get("region", "us-east-1")

    # Scoped session policy: limited to this deployment's namespace
    session_policy = jsonencode_policy({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ScopeToDeploymentNamespace",
                "Effect": "Allow",
                "Action": "*",
                "Resource": f"arn:aws:*:*:*:*terraagent-{deployment_id}*"
            }
        ]
    })

    ak, sk, st = assume_role(
        role_arn=role_arn,
        access_key=access_key,
        secret_key=secret_key,
        session_token=session_token,
        region=region,
        session_name=f"terraagent-apply-{deployment_id}"[:64],
        external_id=external_id,
        source_identity=approver_email[:64],
        tags=[
            {"Key": "deployment_id", "Value": deployment_id},
            {"Key": "approver", "Value": approver_email[:64]},
        ],
        policy=session_policy,
        duration_seconds=3600,
    )

    return {
        "AWS_ACCESS_KEY_ID": ak,
        "AWS_SECRET_ACCESS_KEY": sk,
        "AWS_SESSION_TOKEN": st,
        "AWS_DEFAULT_REGION": region,
        "AWS_REGION": region,
    }


def jsonencode_policy(doc: Dict[str, Any]) -> str:
    return json.dumps(doc, separators=(",", ":"))
