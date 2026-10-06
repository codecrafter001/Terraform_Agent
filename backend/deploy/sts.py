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

    session_policy = plan_session_policy(state_bucket, target_id, deployment_id)

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

    session_policy = apply_session_policy(target.get("state_bucket", ""), target.get("id", "default"), deployment_id)

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


# AWS rejects inline session policies longer than this (plaintext characters).
MAX_SESSION_POLICY_CHARS = 2048

_PLAN_READ_ACTIONS = [
    "s3:Get*", "s3:List*", "lambda:Get*", "lambda:List*", "apigateway:GET",
    "cloudfront:Get*", "cloudfront:List*", "cloudfront:Describe*", "logs:Describe*", "logs:List*",
    "iam:Get*", "iam:List*", "ec2:Describe*", "ecs:Describe*", "ecs:List*",
    "ecr:Describe*", "ecr:List*", "ecr:GetLifecyclePolicy", "ecr:GetRepositoryPolicy",
    "elasticloadbalancing:Describe*", "codebuild:BatchGet*", "codebuild:List*",
    "codepipeline:Get*", "codepipeline:List*", "events:Describe*", "events:List*",
    "rds:Describe*", "rds:ListTagsForResource", "secretsmanager:DescribeSecret", "secretsmanager:GetResourcePolicy",
    "elasticache:Describe*", "elasticache:List*", "application-autoscaling:Describe*",
    "servicequotas:GetServiceQuota",  # VPC quota pre-check (deploy/preflight.py)
]


def _state_resources(state_bucket: str, target_id: str, deployment_id: str) -> list:
    return [
        f"arn:aws:s3:::{state_bucket}",
        f"arn:aws:s3:::{state_bucket}/terraagent/{target_id}/{deployment_id}.tfstate*",
    ]


def plan_session_policy(state_bucket: str, target_id: str, deployment_id: str) -> str:
    """Read-only describe access for terraform plan/refresh, plus this deployment's
    state object (read + lock). No secret values: secretsmanager is metadata-only."""
    policy = jsonencode_policy({
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "State", "Effect": "Allow",
             "Action": ["s3:GetObject", "s3:ListBucket", "s3:PutObject", "s3:DeleteObject"],
             "Resource": _state_resources(state_bucket, target_id, deployment_id)},
            {"Sid": "Read", "Effect": "Allow", "Action": _PLAN_READ_ACTIONS, "Resource": "*"},
        ],
    })
    if len(policy) > MAX_SESSION_POLICY_CHARS:
        raise ValueError(f"plan session policy is {len(policy)} characters; AWS allows {MAX_SESSION_POLICY_CHARS}")
    return policy


def apply_session_policy(state_bucket: str, target_id: str, deployment_id: str) -> str:
    """What one apply may touch, intersected with the apply role's own policy:
    - anything named terraagent-<deployment_id>* (S3, IAM roles, ECS, ECR, ELB, RDS,
      logs, CodeBuild/CodePipeline, EventBridge, Secrets Manager, Lambda), and its state;
    - ID-named services (EC2 networking, CloudFront, task definitions, API Gateway)
      only when the request or the resource carries this deployment's tag;
    - read-only describes, service-linked roles, CloudFront origin access controls,
      security-group rules (always under a tag-checked group), and the RDS-managed
      database secret (create/tag/rotate/delete - never read)."""
    tag = "terraagent:deployment-id"
    tagged_services = ["ec2:*", "cloudfront:*", "ecs:*", "apigateway:*", "application-autoscaling:*"]
    policy = jsonencode_policy({
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "Named", "Effect": "Allow", "Action": "*",
             "Resource": [f"arn:aws:*:*:*:*terraagent-{deployment_id}*",
                          *_state_resources(state_bucket, target_id, deployment_id)]},
            {"Sid": "TaggedReq", "Effect": "Allow", "Action": tagged_services, "Resource": "*",
             "Condition": {"StringEquals": {f"aws:RequestTag/{tag}": deployment_id}}},
            {"Sid": "TaggedRes", "Effect": "Allow", "Action": tagged_services, "Resource": "*",
             "Condition": {"StringEquals": {f"aws:ResourceTag/{tag}": deployment_id}}},
            {"Sid": "Read", "Effect": "Allow", "Resource": "*",
             "Action": ["ec2:Describe*", "ecs:Describe*", "ecs:List*", "ecs:DeregisterTaskDefinition",
                        "elasticloadbalancing:Describe*", "rds:Describe*", "cloudfront:Get*", "cloudfront:List*",
                        "cloudfront:Describe*", "cloudfront:*OriginAccessControl", "kms:DescribeKey",
                        "elasticache:Describe*", "application-autoscaling:Describe*",
                        # DescribeLogGroups is account-scoped (log-group::log-stream:), so the
                        # Named statement never matches it: terraform couldn't read back the log
                        # groups it had just created, and they were tainted on every apply.
                        "logs:DescribeLogGroups", "logs:ListTagsForResource", "logs:ListTagsLogGroup"]},
            {"Sid": "Helpers", "Effect": "Allow", "Action": ["ec2:*SecurityGroup*", "iam:CreateServiceLinkedRole",
                                                              "secretsmanager:CreateSecret", "secretsmanager:TagResource",
                                                              "secretsmanager:RotateSecret", "secretsmanager:DeleteSecret",
                                                              "secretsmanager:DescribeSecret"],
             "Resource": ["arn:aws:ec2:*:*:security-group-rule/*", "arn:aws:iam::*:role/aws-service-role/*",
                          "arn:aws:secretsmanager:*:*:secret:rds!*"]},
        ],
    })
    if len(policy) > MAX_SESSION_POLICY_CHARS:
        raise ValueError(f"apply session policy is {len(policy)} characters; AWS allows {MAX_SESSION_POLICY_CHARS}")
    return policy
