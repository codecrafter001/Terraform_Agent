"""Shared STS AssumeRole helper - used by cloud_discovery_node (per-scan
multi-account access) and the AWS Organizations account listing endpoint
(which needs a management-account role to call organizations:ListAccounts).
"""

from typing import Optional, Tuple

import boto3


def assume_role(
    role_arn: str, access_key: str, secret_key: str, session_token: Optional[str],
    region: str, session_name: str, endpoint_url: Optional[str] = None
) -> Tuple[str, str, str]:
    """Exchanges the caller's credentials for a short-lived (1 hour) set
    scoped to role_arn. Returns (access_key, secret_key, session_token) as
    plain local values - callers must not persist these beyond the
    operation they were requested for."""
    sts_session = boto3.Session(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token,
        region_name=region
    )
    sts = sts_session.client("sts", endpoint_url=endpoint_url)
    response = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName=session_name[:64],
        DurationSeconds=3600
    )
    creds = response["Credentials"]
    return creds["AccessKeyId"], creds["SecretAccessKey"], creds["SessionToken"]
