"""Plan policy evaluation for deployment mode (design doc §9.3).

Validates terraform plan JSON against safety rules before presenting the plan
to a human approver:
- Target-specific resource allowlists
- Deployment ID tagging / naming namespace
- Wildcard IAM policy restrictions
- Resource count (<= 50) and monthly cost (<= $200) limits
- Destructive change tier detection
"""

import json
from typing import Any, Dict, List, Optional, Set

ALLOWED_TARGET_RESOURCES: Dict[str, Set[str]] = {
    "static_site": {
        "aws_s3_bucket",
        "aws_s3_bucket_public_access_block",
        "aws_s3_bucket_policy",
        "aws_s3_bucket_ownership_controls",
        "aws_s3_bucket_website_configuration",
        "aws_s3_bucket_versioning",
        "aws_s3_bucket_server_side_encryption_configuration",
        "aws_s3_object",
        "aws_cloudfront_distribution",
        "aws_cloudfront_origin_access_control",
    },
    "lambda_http": {
        "aws_lambda_function",
        "aws_lambda_alias",
        "aws_lambda_function_url",
        "aws_lambda_permission",
        "aws_iam_role",
        "aws_iam_role_policy",
        "aws_iam_role_policy_attachment",
        "aws_cloudwatch_log_group",
        "aws_apigatewayv2_api",
        "aws_apigatewayv2_stage",
        "aws_apigatewayv2_route",
        "aws_apigatewayv2_integration",
    },
    "ecs_service": {
        "aws_ecr_repository",
        "aws_ecr_lifecycle_policy",
        "aws_codebuild_project",
        "aws_ecs_cluster",
        "aws_ecs_task_definition",
        "aws_ecs_service",
        "aws_lb",
        "aws_lb_target_group",
        "aws_lb_listener",
        "aws_security_group",
        "aws_security_group_rule",
        "aws_iam_role",
        "aws_iam_role_policy",
        "aws_iam_role_policy_attachment",
        "aws_cloudwatch_log_group",
    },
}

MAX_RESOURCE_COUNT_DEFAULT = 50
MAX_MONTHLY_COST_DEFAULT = 200.0


def evaluate_plan_policy(
    plan_json: Dict[str, Any],
    target_type: str,
    deployment_id: str,
    monthly_cost: Optional[float] = None,
    max_resources: int = MAX_RESOURCE_COUNT_DEFAULT,
    max_cost: float = MAX_MONTHLY_COST_DEFAULT,
) -> Dict[str, Any]:
    """Evaluates the plan against security and sanity policies.
    Returns:
    {
        "passed": bool,
        "violations": List[str],
        "warnings": List[str],
        "is_destructive": bool,
        "destructive_changes": List[Dict[str, str]],
        "summary": {
            "resource_count": int,
            "target_type": str,
            "target_allowed": bool,
        }
    }
    """
    violations: List[str] = []
    warnings: List[str] = []
    destructive_changes: List[Dict[str, str]] = []

    resource_changes = plan_json.get("resource_changes", []) or []
    allowed_types = ALLOWED_TARGET_RESOURCES.get(target_type)

    total_changes = 0

    for change in resource_changes:
        res_type = change.get("type", "")
        address = change.get("address", "unknown")
        actions = change.get("change", {}).get("actions", [])
        after = change.get("change", {}).get("after") or {}

        if actions in (["no-op"], ["read"]):
            continue
        if change.get("mode") == "data":
            continue

        total_changes += 1

        # 1. Target Resource Type Allowlist
        if allowed_types is not None and res_type not in allowed_types:
            violations.append(
                f"Resource type '{res_type}' at {address} is not allowed for target '{target_type}'."
            )

        # 2. Tagging & Naming scoping
        # Check tags or name if create action
        if "create" in actions:
            tags = after.get("tags") or after.get("tags_all") or {}
            dep_tag = tags.get("terraagent:deployment-id")
            res_name = after.get("name") or after.get("bucket") or after.get("function_name") or ""

            # Check tag or naming prefix
            if not dep_tag and not res_name.startswith(f"terraagent-{deployment_id}") and not res_name.startswith("terraagent-"):
                warnings.append(
                    f"Resource {address} should carry tag 'terraagent:deployment-id' or name prefix 'terraagent-'."
                )

        # 3. IAM Policy Document Inspection
        if res_type in ("aws_iam_role_policy", "aws_iam_policy"):
            raw_policy = after.get("policy")
            if raw_policy:
                _check_iam_policy(raw_policy, address, violations)

        # 4. Destructive change detection (replace or destroy)
        if "delete" in actions and "create" in actions:
            destructive_changes.append({"address": address, "action": "replace", "type": res_type})
        elif "delete" in actions:
            destructive_changes.append({"address": address, "action": "delete", "type": res_type})

    # 5. Resource Count Limit (D7)
    if total_changes > max_resources:
        violations.append(
            f"Plan modifies {total_changes} resources, exceeding the maximum allowed limit of {max_resources}."
        )

    # 6. Cost Limit (D7)
    if monthly_cost is not None and monthly_cost > max_cost:
        violations.append(
            f"Estimated monthly cost (${monthly_cost:.2f}) exceeds the default threshold (${max_cost:.2f})."
        )

    is_destructive = len(destructive_changes) > 0
    passed = len(violations) == 0

    return {
        "passed": passed,
        "violations": violations,
        "warnings": warnings,
        "is_destructive": is_destructive,
        "destructive_changes": destructive_changes,
        "summary": {
            "resource_count": total_changes,
            "target_type": target_type,
            "target_allowed": allowed_types is not None,
        },
    }


def _check_iam_policy(policy_str: Any, address: str, violations: List[str]) -> None:
    try:
        doc = json.loads(policy_str) if isinstance(policy_str, str) else policy_str
        statements = doc.get("Statement", [])
        if isinstance(statements, dict):
            statements = [statements]

        for stmt in statements:
            if stmt.get("Effect") != "Allow":
                continue

            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]

            principal = stmt.get("Principal", {})

            # Wildcard action check
            if "*" in actions or "iam:*" in actions:
                violations.append(f"IAM policy at {address} contains forbidden wildcard Action ('*' or 'iam:*').")

            # Wildcard principal check (allow only specific OAC Service principal or non-wildcard)
            if principal == "*" or (isinstance(principal, dict) and principal.get("AWS") == "*"):
                # Exception: CloudFront OAC Service principal condition check
                cond = stmt.get("Condition", {})
                if not cond.get("StringEquals", {}).get("AWS:SourceArn"):
                    violations.append(f"IAM policy at {address} contains unrestricted Principal '*'.")
    except Exception:
        pass
