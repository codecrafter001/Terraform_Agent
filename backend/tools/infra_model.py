"""Canonical Infra Model - the Infrastructure Agent's output.

One record per discovered resource: type, ARN, Terraform import ID, region,
attributes, dependencies, stack, adoption decision (manage / reference /
exclude / review) and evidence (which AWS API it came from, when, and which
classification rule decided it). Every later agent reads this model instead of
re-deriving facts from raw discovery output.

Deterministic, no LLM. Import IDs come from a fixed lookup table
(IMPORT_ID_FIELDS), never from a model.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

MODEL_VERSION = 1

# Terraform import ID per resource type: which discovered field holds it.
# Must match the AWS provider's documented `terraform import` / `import {}` ID.
IMPORT_ID_FIELDS: Dict[str, str] = {
    "aws_vpc": "id",
    "aws_subnet": "id",
    "aws_route_table": "id",
    "aws_security_group": "id",
    "aws_instance": "id",
    "aws_s3_bucket": "id",  # bucket name
    "aws_db_instance": "id",  # DB instance identifier
    "aws_iam_role": "name",  # role name, not ARN
}

# The read-only API each resource type is discovered through (tools/aws_scanner.py).
SOURCE_API: Dict[str, str] = {
    "aws_vpc": "ec2:DescribeVpcs",
    "aws_subnet": "ec2:DescribeSubnets",
    "aws_route_table": "ec2:DescribeRouteTables",
    "aws_security_group": "ec2:DescribeSecurityGroups",
    "aws_instance": "ec2:DescribeInstances",
    "aws_s3_bucket": "s3:ListBuckets",
    "aws_db_instance": "rds:DescribeDBInstances",
    "aws_iam_role": "iam:ListRoles",
}

_NON_ATTRIBUTE_KEYS = {"id", "name", "resource_type", "tags", "arn", "region"}


def import_id_for(resource: Dict[str, Any]) -> Optional[str]:
    field = IMPORT_ID_FIELDS.get(str(resource.get("resource_type") or ""))
    value = resource.get(field) if field else None
    return str(value) if value else None


def _tags(resource: Dict[str, Any]) -> Dict[str, str]:
    tags = resource.get("tags") or []
    if isinstance(tags, dict):
        return {str(k): str(v) for k, v in tags.items()}
    return {
        str(t.get("Key") or t.get("key")): str(t.get("Value") or t.get("value") or "")
        for t in tags
        if isinstance(t, dict) and (t.get("Key") or t.get("key"))
    }


def build_infra_model(
    resources: List[Dict[str, Any]],
    dependency_graph: Dict[str, Any],
    classification: Dict[str, Any],
    region: str,
    discovered_at: Optional[str] = None,
) -> Dict[str, Any]:
    discovered_at = discovered_at or datetime.utcnow().isoformat()
    graph = dependency_graph or {}

    depends_on: Dict[str, List[str]] = {}
    for link in graph.get("links", []) or []:
        if isinstance(link, dict) and link.get("source") and link.get("target"):
            # Edges run dependency -> dependent (e.g. vpc -> subnet).
            depends_on.setdefault(str(link["target"]), []).append(str(link["source"]))

    stack_of: Dict[str, str] = {}
    for stack in graph.get("stacks", []) or []:
        for rid in stack.get("resource_ids", []) or []:
            stack_of[str(rid)] = stack.get("name", "")

    decisions = {
        c.get("resource_id"): c
        for c in (classification or {}).get("classifications", []) or []
        if isinstance(c, dict)
    }

    records: List[Dict[str, Any]] = []
    counts = {"manage": 0, "reference": 0, "exclude": 0, "review": 0}
    for res in resources or []:
        if not isinstance(res, dict) or not res.get("id"):
            continue
        rid = str(res["id"])
        rtype = str(res.get("resource_type") or "unknown")
        c = decisions.get(rid, {})
        decision = c.get("decision") or "review"
        counts[decision] = counts.get(decision, 0) + 1
        records.append({
            "id": rid,
            "type": rtype,
            "name": res.get("name"),
            "arn": res.get("arn"),
            "import_id": import_id_for(res),
            "region": res.get("region") or region,
            "attributes": {k: v for k, v in res.items() if k not in _NON_ATTRIBUTE_KEYS},
            "tags": _tags(res),
            "dependencies": sorted(set(depends_on.get(rid, []))),
            "stack": stack_of.get(rid),
            "decision": decision,
            "category": c.get("category"),
            "reasons": c.get("reason", []),
            "evidence": {
                "source_api": SOURCE_API.get(rtype),
                "discovered_at": discovered_at,
                **(c.get("evidence") or {}),
            },
        })

    records.sort(key=lambda r: (r["type"], r["id"]))
    return {
        "version": MODEL_VERSION,
        "region": region,
        "generated_at": discovered_at,
        "summary": {"total": len(records), **counts},
        "records": records,
    }
