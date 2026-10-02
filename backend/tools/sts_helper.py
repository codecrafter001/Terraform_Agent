"""Shared STS AssumeRole helper - used by cloud_discovery_node (per-scan
multi-account access), the AWS Organizations account listing endpoint,
and deployment mode (Phase 3 STS sessions).
"""

from typing import Any, Dict, List, Optional, Tuple

import boto3


def assume_role(
    role_arn: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    session_token: Optional[str] = None,
    region: str = "us-east-1",
    session_name: str = "terraagent-session",
    endpoint_url: Optional[str] = None,
    external_id: Optional[str] = None,
    source_identity: Optional[str] = None,
    tags: Optional[List[Dict[str, str]]] = None,
    policy: Optional[str] = None,
    duration_seconds: int = 3600,
) -> Tuple[str, str, str]:
    """Exchanges credentials for a short-lived set scoped to role_arn.
    Returns (access_key, secret_key, session_token) as plain local values.
    Callers must not persist these beyond the operation they were requested for.
    """
    if access_key and secret_key:
        sts_session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
            region_name=region,
        )
    else:
        sts_session = boto3.Session(region_name=region)

    sts = sts_session.client("sts", endpoint_url=endpoint_url)

    kwargs: Dict[str, Any] = {}
    if external_id:
        kwargs["ExternalId"] = external_id
    if source_identity:
        kwargs["SourceIdentity"] = source_identity[:64]
    if tags:
        kwargs["Tags"] = [{"Key": str(t.get("Key", t.get("key", ""))), "Value": str(t.get("Value", t.get("value", "")))} for t in tags]
    if policy:
        kwargs["Policy"] = policy

    response = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName=session_name[:64],
        DurationSeconds=max(900, min(duration_seconds, 43200)),
        **kwargs,
    )
    creds = response["Credentials"]
    return creds["AccessKeyId"], creds["SecretAccessKey"], creds["SessionToken"]
