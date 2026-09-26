"""Intent Analyzer tool for processing DevOps natural-language requests into structured intent."""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from services.ollama_client import LLMClient

logger = logging.getLogger("terraagent.tools.intent_analyzer")

PROMPT_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "prompts",
    "intent_analysis.txt",
)

OPERATION_LABELS = {
    "modify": "Modify Infrastructure",
    "generate": "Generate IaC Code",
    "scan": "Scan & Discover",
    "explain": "Explain Topology",
    "fix": "Fix / Remediate",
    "validate": "Validate & Audit",
}

RESOURCE_CATEGORY_MAP = {
    "ec2": "EC2",
    "instance": "EC2",
    "server": "EC2",
    "fargate": "ECS",
    "ecs": "ECS",
    "container": "ECS",
    "task": "ECS",
    "eks": "EKS",
    "rds": "RDS",
    "database": "RDS",
    "postgres": "RDS",
    "mysql": "RDS",
    "s3": "S3",
    "bucket": "S3",
    "storage": "S3",
    "vpc": "VPC",
    "subnet": "VPC",
    "gateway": "VPC",
    "igw": "VPC",
    "nat": "VPC",
    "sg": "SG",
    "security group": "SG",
    "firewall": "SG",
    "iam": "IAM",
    "role": "IAM",
    "policy": "IAM",
    "lambda": "LAMBDA",
    "function": "LAMBDA",
}


def parse_deterministic_intent(
    user_request: str,
    region: str = "us-east-1",
    environment: str = "production",
    resource_filters: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Fallback deterministic rule-based NLP intent parser."""
    text = (user_request or "").strip()
    lower_text = text.lower()

    # 1. Classify operation
    operation = "generate"
    if any(k in lower_text for k in ["increase", "scale", "decrease", "resize", "change", "upgrade", "downgrade", "modify", "update", "set "]):
        operation = "modify"
    elif any(k in lower_text for k in ["fix", "remediate", "patch", "repair", "close port", "harden"]):
        operation = "fix"
    elif any(k in lower_text for k in ["validate", "lint", "check policy", "tfsec", "checkov", "opa"]):
        operation = "validate"
    elif any(k in lower_text for k in ["explain", "describe", "why", "diagram", "show graph"]):
        operation = "explain"
    elif any(k in lower_text for k in ["scan only", "inventory", "discover", "list resources"]):
        operation = "scan"
    elif any(k in lower_text for k in ["create", "generate", "build", "synthesize", "new"]):
        operation = "generate"

    target_resources: List[Dict[str, Any]] = []
    requested_changes: List[Dict[str, Any]] = []
    suggested_filters: List[str] = list(resource_filters or [])

    # Pattern: EC2 instance type change (e.g. "increase EC2 web server from t2.micro to t2.medium")
    ec2_match = re.search(r'(?:increase|scale|change|resize|modify|update)?\s*(?:ec2\s+)?([a-zA-Z0-9_\-\s]+?)\s+(?:from\s+)?([trmc][0-9][a-z]?\.[a-z0-9]+)\s+to\s+([trmc][0-9][a-z]?\.[a-z0-9]+)', text, re.IGNORECASE)
    if ec2_match:
        name = ec2_match.group(1).strip()
        # Clean prefix noise
        name = re.sub(r'^(?:increase|scale|change|resize|modify|update|ec2|instance|server)\s+', '', name, flags=re.IGNORECASE).strip()
        if not name or name.lower() in ("from", "to", "the", "a", "an"):
            name = "web server"
        c_val = ec2_match.group(2).strip()
        t_val = ec2_match.group(3).strip()
        target_resources.append({
            "resource_type": "aws_instance",
            "resource_name": name,
            "category": "EC2",
        })
        requested_changes.append({
            "resource": f"EC2 {name}",
            "attribute": "instance_type",
            "current_value": c_val,
            "target_value": t_val,
            "action": "resize",
        })
        if "EC2" not in suggested_filters:
            suggested_filters.append("EC2")

    # Pattern: Fargate / ECS task scaling (e.g. "scale Fargate from 2 to 4 tasks")
    fargate_match = re.search(r'(?:scale\s+)?(?:fargate|ecs|task(?:s)?)(?:\s+service)?\s+(?:from\s+)?(\d+)\s+to\s+(\d+)(?:\s+tasks)?', text, re.IGNORECASE)
    if fargate_match:
        c_val = fargate_match.group(1).strip()
        t_val = fargate_match.group(2).strip()
        target_resources.append({
            "resource_type": "aws_ecs_service",
            "resource_name": "Fargate Service",
            "category": "ECS",
        })
        requested_changes.append({
            "resource": "ECS Fargate Service",
            "attribute": "desired_count",
            "current_value": c_val,
            "target_value": t_val,
            "action": "scale_out" if int(t_val) > int(c_val) else "scale_in",
        })
        if "ECS" not in suggested_filters:
            suggested_filters.append("ECS")

    # Pattern: RDS scaling (e.g. "resize RDS db from db.t3.micro to db.t3.large")
    rds_match = re.search(r'(?:rds|db|database)\s+(?:from\s+)?(db\.[a-z0-9\.]+)\s+to\s+(db\.[a-z0-9\.]+)', text, re.IGNORECASE)
    if rds_match:
        c_val = rds_match.group(1).strip()
        t_val = rds_match.group(2).strip()
        target_resources.append({
            "resource_type": "aws_db_instance",
            "resource_name": "RDS Database",
            "category": "RDS",
        })
        requested_changes.append({
            "resource": "RDS Database",
            "attribute": "instance_class",
            "current_value": c_val,
            "target_value": t_val,
            "action": "resize",
        })
        if "RDS" not in suggested_filters:
            suggested_filters.append("RDS")

    # Pattern: S3 bucket configuration / creation
    if "s3" in lower_text or "bucket" in lower_text:
        s3_bucket_match = re.search(r'(?:s3\s+bucket|bucket)\s+([a-zA-Z0-9.\-_]+)', text, re.IGNORECASE)
        b_name = s3_bucket_match.group(1) if s3_bucket_match else "S3 Bucket"
        if not any(r["resource_name"] == b_name for r in target_resources):
            target_resources.append({
                "resource_type": "aws_s3_bucket",
                "resource_name": b_name,
                "category": "S3",
            })
            if "S3" not in suggested_filters:
                suggested_filters.append("S3")

    # Pattern: General fallback if no specific regex triggered
    if not target_resources:
        for kw, cat in RESOURCE_CATEGORY_MAP.items():
            if kw in lower_text:
                res_type = f"aws_{cat.lower()}"
                if not any(r["category"] == cat for r in target_resources):
                    target_resources.append({
                        "resource_type": res_type,
                        "resource_name": f"{cat} Resource",
                        "category": cat,
                    })
                if cat not in suggested_filters:
                    suggested_filters.append(cat)

    if not requested_changes and text:
        requested_changes.append({
            "resource": target_resources[0]["resource_name"] if target_resources else "AWS Infrastructure",
            "attribute": "configuration",
            "current_value": "current state",
            "target_value": text[:80] + ("..." if len(text) > 80 else ""),
            "action": operation,
        })

    # Risk level determination
    risk_level = "low"
    if operation in ("modify", "fix"):
        risk_level = "medium"
        if any("db" in r.get("resource_type", "") or "rds" in r.get("category", "").lower() for r in target_resources):
            risk_level = "high"
    elif operation == "generate":
        risk_level = "low"

    summary = (
        f"Identified '{OPERATION_LABELS.get(operation, operation)}' intent for {len(target_resources)} resource(s) "
        f"in {environment} ({region})."
    )

    return {
        "operation": operation,
        "operation_label": OPERATION_LABELS.get(operation, operation.title()),
        "target_resources": target_resources,
        "requested_changes": requested_changes,
        "confidence_score": 0.92 if (ec2_match or fargate_match or rds_match) else 0.85,
        "summary": summary,
        "environment": environment,
        "region": region,
        "risk_level": risk_level,
        "suggested_filters": suggested_filters or ["EC2", "VPC", "S3", "RDS", "IAM", "SG"],
    }


async def analyze_user_intent(
    user_request: str,
    region: str = "us-east-1",
    environment: str = "production",
    resource_filters: Optional[List[str]] = None,
    llm_client: Optional[LLMClient] = None,
) -> Dict[str, Any]:
    """Analyzes a DevOps natural language request using LLM with deterministic fallback."""
    if not user_request or not user_request.strip():
        return {
            "operation": "generate",
            "operation_label": OPERATION_LABELS["generate"],
            "target_resources": [],
            "requested_changes": [],
            "confidence_score": 1.0,
            "summary": f"Standard infrastructure discovery and generation for {environment} ({region}).",
            "environment": environment,
            "region": region,
            "risk_level": "low",
            "suggested_filters": resource_filters or ["EC2", "VPC", "S3", "RDS", "IAM", "SG"],
        }

    # Prepare prompt if template exists
    prompt_str = ""
    if os.path.exists(PROMPT_TEMPLATE_PATH):
        try:
            with open(PROMPT_TEMPLATE_PATH, "r", encoding="utf-8") as f:
                template = f.read()
            prompt_str = template.format(
                region=region,
                environment=environment,
                resource_filters=", ".join(resource_filters or ["All"]),
                user_request=user_request.strip(),
            )
        except Exception as e:
            logger.warning(f"Could not read intent prompt template: {e}")

    client = llm_client or LLMClient()
    if prompt_str:
        try:
            raw_response = await client.generate_json(
                prompt=prompt_str,
                system="You are an expert AWS DevOps Infrastructure Architect. Always respond with strict valid JSON only.",
                temperature=0.1,
            )
            if isinstance(raw_response, dict) and "operation" in raw_response and "target_resources" in raw_response:
                op = str(raw_response.get("operation", "generate")).lower()
                if op not in OPERATION_LABELS:
                    op = "modify" if "scale" in user_request.lower() or "increase" in user_request.lower() else "generate"
                raw_response["operation"] = op
                raw_response["operation_label"] = OPERATION_LABELS.get(op, op.title())
                raw_response["environment"] = environment
                raw_response["region"] = region
                if "risk_level" not in raw_response:
                    raw_response["risk_level"] = "medium" if op in ("modify", "fix") else "low"
                if "suggested_filters" not in raw_response or not raw_response["suggested_filters"]:
                    raw_response["suggested_filters"] = resource_filters or ["EC2", "VPC", "S3", "RDS", "IAM", "SG"]
                return raw_response
        except Exception as e:
            logger.info(f"LLM intent generation exception ({e}), falling back to deterministic parser.")

    return parse_deterministic_intent(
        user_request=user_request,
        region=region,
        environment=environment,
        resource_filters=resource_filters,
    )
