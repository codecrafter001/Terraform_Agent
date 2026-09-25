"""Deterministic resource classification - no LLM, no new external calls.

Rules first: every resource gets an adoption decision - manage / reference /
exclude / review - plus the category that explains it (managed / unmanaged /
orphaned / shared / unsupported), per the decision-tree design in
docs/design/cloud-inventory-and-adoption-planning.md. This is the agent
that makes the eventual Adoption Plan (and the composer's skip/data-source
branches) possible - everything here only reads fields already present in
discovered resource dicts (tools/aws_scanner.py) and the dependency graph
(tools/graph_builder.py); it adds no new AWS API calls.
"""

import os
from typing import Any, Dict, List, Optional, Tuple

from models.adoption import DECISION_TO_ACTION, ClassificationReport, ResourceClassification

# IAM is high-risk to adopt: by default a (non service-linked, non shared) IAM
# role is sent to Review rather than managed. Set to "true" to manage them.
MANAGE_IAM = os.getenv("TERRAAGENT_MANAGE_IAM", "false").strip().lower() in ("1", "true", "yes")

# Exactly the 8 resource types AWSScanner (tools/aws_scanner.py) discovers today.
# Kept as an explicit whitelist rather than an implicit "not vpc/subnet/..." check
# so this degrades safely (falls into "unsupported", never crashes) the day
# discovery adds a 9th type before this classifier is updated to match.
DISCOVERY_RESOURCE_TYPES = {
    "aws_vpc", "aws_subnet", "aws_route_table", "aws_security_group",
    "aws_instance", "aws_s3_bucket", "aws_db_instance", "aws_iam_role",
}


def _iac_owner(resource: Dict[str, Any]) -> Optional[str]:
    """"cloudformation" / "terraform" if another IaC tool already owns this
    resource (detected from fixed tag keys or explicit metadata), else None."""
    if not isinstance(resource, dict):
        return None
    tags = resource.get("tags") or []
    pairs: List[Tuple[str, str]] = []
    if isinstance(tags, list):
        pairs = [
            (str(t.get("Key") or t.get("key") or "").strip().lower(), str(t.get("Value") or t.get("value") or ""))
            for t in tags if isinstance(t, dict)
        ]
    elif isinstance(tags, dict):
        pairs = [(str(k).strip().lower(), str(v)) for k, v in tags.items()]
    if any(k in ("aws:cloudformation:stack-name", "aws:cloudformation:stack-id") for k, _ in pairs):
        return "cloudformation"
    if _is_managed_by_terraform(resource):
        return "terraform"
    return None


def _is_managed_by_terraform(resource: Dict[str, Any]) -> bool:
    """Checks if a resource is already managed by Terraform, OpenTofu, or IaC."""
    if not isinstance(resource, dict):
        return False
    if resource.get("is_managed") is True or resource.get("managed") is True:
        return True

    # Check tags for IaC / Terraform management indicators
    tags = resource.get("tags") or []
    if isinstance(tags, list):
        for t in tags:
            if not isinstance(t, dict):
                continue
            k = str(t.get("Key") or t.get("key") or "").strip().lower()
            v = str(t.get("Value") or t.get("value") or "").strip().lower()
            if k in ("terraform", "managedby", "managed_by", "managed-by", "iac", "creator", "created_by"):
                if v in ("true", "terraform", "opentofu", "tofu", "yes", "1") or "terraform" in v or "opentofu" in v:
                    return True
            if k in ("aws:cloudformation:stack-name", "aws:cloudformation:stack-id"):
                return True
    elif isinstance(tags, dict):
        for k_raw, v_raw in tags.items():
            k = str(k_raw).strip().lower()
            v = str(v_raw).strip().lower()
            if k in ("terraform", "managedby", "managed_by", "managed-by", "iac", "creator", "created_by"):
                if v in ("true", "terraform", "opentofu", "tofu", "yes", "1") or "terraform" in v or "opentofu" in v:
                    return True
            if k in ("aws:cloudformation:stack-name", "aws:cloudformation:stack-id"):
                return True
    return False


def _is_service_linked_role(resource: Dict[str, Any]) -> bool:
    name = resource.get("name") or ""
    arn = resource.get("arn") or ""
    path = resource.get("path") or ""
    return name.startswith("AWSServiceRoleFor") or "/aws-service-role/" in arn or "/aws-service-role/" in path


def _is_shared_resource(resource: Dict[str, Any]) -> Tuple[bool, str, str]:
    """Returns (is_shared, recommended_action, reason)."""
    if not isinstance(resource, dict):
        return False, "", ""

    r_type = resource.get("resource_type") or ""
    name = resource.get("name") or ""
    group_name = resource.get("group_name") or ""

    if r_type == "aws_security_group" and (name == "default" or group_name == "default"):
        return True, "data_source", "AWS auto-creates a default security group for every VPC - not something to import"

    if r_type == "aws_vpc" and resource.get("is_default") is True:
        return True, "data_source", "AWS default VPC - reference via data source rather than direct ownership"

    if r_type == "aws_iam_role" and _is_service_linked_role(resource):
        return True, "skip", "AWS service-linked IAM role, managed by the AWS service itself"

    if resource.get("is_shared") is True or resource.get("shared") is True:
        return True, "data_source", "Resource is marked as shared across environments or accounts"

    tags = resource.get("tags") or []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, dict):
                k = str(t.get("Key") or t.get("key") or "").strip().lower()
                v = str(t.get("Value") or t.get("value") or "").strip().lower()
                if k in ("shared", "is_shared") and v in ("true", "yes", "1"):
                    return True, "data_source", "Resource has a Shared tag indicating multi-stack usage"
    elif isinstance(tags, dict):
        for k_raw, v_raw in tags.items():
            k = str(k_raw).strip().lower()
            v = str(v_raw).strip().lower()
            if k in ("shared", "is_shared") and v in ("true", "yes", "1"):
                return True, "data_source", "Resource has a Shared tag indicating multi-stack usage"

    return False, "", ""


def _build_degree_map(dependency_graph: Dict[str, Any]) -> Dict[str, int]:
    """Node id -> number of edges touching it, from dependency_graph.links
    (tools/graph_builder.py's output shape). A degree of 0 for a node that
    genuinely exists in the graph is the "orphaned" signal."""
    if not isinstance(dependency_graph, dict):
        return {}
    degree: Dict[str, int] = {n["id"]: 0 for n in dependency_graph.get("nodes", []) if isinstance(n, dict) and "id" in n}
    for link in dependency_graph.get("links", []) or []:
        if not isinstance(link, dict):
            continue
        source, target = link.get("source"), link.get("target")
        if source in degree:
            degree[source] += 1
        if target in degree:
            degree[target] += 1
    return degree


def _build_trusted_services_map(dependency_graph: Dict[str, Any]) -> Dict[str, List[str]]:
    """Node id -> trusted_by_service_types, from dependency_graph.nodes
    (tools/graph_builder.py's output shape, populated only for aws_iam_role
    nodes with a parsed trust policy)."""
    if not isinstance(dependency_graph, dict):
        return {}
    return {
        n["id"]: n["trusted_by_service_types"]
        for n in dependency_graph.get("nodes", []) or []
        if isinstance(n, dict) and n.get("trusted_by_service_types") and "id" in n
    }


def classify_resources(resources: List[Dict[str, Any]], dependency_graph: Dict[str, Any]) -> ClassificationReport:
    """Gives every resource a decision (manage / reference / exclude / review)
    and the category behind it:
    - unsupported type -> exclude (malformed data -> review)
    - CloudFormation-managed -> exclude; Terraform-managed elsewhere -> reference
    - service-linked role -> exclude; AWS default VPC/SG -> reference if something
      depends on it, else exclude; shared marker -> reference
    - IAM role -> review (unless TERRAAGENT_MANAGE_IAM)
    - orphaned in the dependency graph -> review
    - otherwise unmanaged -> manage
    """
    degree = _build_degree_map(dependency_graph) if dependency_graph else {}
    trusted_services = _build_trusted_services_map(dependency_graph) if dependency_graph else {}

    classifications: List[ResourceClassification] = []
    summary: Dict[str, int] = {
        "managed": 0,
        "unmanaged": 0,
        "shared": 0,
        "orphaned": 0,
        "unsupported": 0,
    }

    decisions: Dict[str, int] = {"manage": 0, "reference": 0, "exclude": 0, "review": 0}

    if not isinstance(resources, list):
        return ClassificationReport(classifications=[], summary=summary, decisions=decisions)

    for idx, res in enumerate(resources):
        if not isinstance(res, dict):
            classifications.append(ResourceClassification(
                resource_id=f"malformed_{idx}",
                resource_type="unknown",
                category="unsupported",
                reason=["malformed discovery data: resource is not a dictionary"],
                recommended_action="manual_review",
                decision="review",
                evidence={"rule": "malformed"},
            ))
            summary["unsupported"] += 1
            decisions["review"] += 1
            continue

        r_id = res.get("id")
        r_type = res.get("resource_type")

        # Check for malformed or missing metadata
        if not r_id:
            classifications.append(ResourceClassification(
                resource_id=f"missing_id_{idx}",
                resource_type=str(r_type or "unknown"),
                category="unsupported",
                reason=["malformed discovery data: missing resource id"],
                recommended_action="manual_review",
                decision="review",
                evidence={"rule": "malformed"},
            ))
            summary["unsupported"] += 1
            decisions["review"] += 1
            continue

        r_id = str(r_id)
        reasons: List[str] = []
        category: Optional[str] = None
        decision: Optional[str] = None
        evidence: Dict[str, Any] = {}

        # 1. No type at all is malformed data (Review); a known type we can't
        #    generate yet is Excluded and listed in the report.
        if not r_type:
            category, decision = "unsupported", "review"
            reasons.append("resource type 'unknown' has no adoption support yet (malformed discovery data)")
            evidence = {"rule": "malformed"}
        elif r_type not in DISCOVERY_RESOURCE_TYPES:
            category, decision = "unsupported", "exclude"
            reasons.append(f"resource type '{r_type}' has no adoption support yet")
            evidence = {"rule": "unsupported_type"}

        # 2. Already owned by another IaC tool.
        if category is None:
            owner = _iac_owner(res)
            if owner == "cloudformation":
                category, decision = "managed", "exclude"
                reasons.append("resource is already managed by a CloudFormation stack - adopting it would fight that stack")
                evidence = {"rule": "cloudformation_tag"}
            elif owner == "terraform":
                category, decision = "managed", "reference"
                reasons.append("resource is already managed by Terraform/IaC elsewhere (detected via tag/metadata) - "
                               "referenced with a data block, not re-imported")
                evidence = {"rule": "terraform_tag_or_flag"}

        # 3. AWS defaults, service-linked roles, shared resources.
        if category is None:
            is_shared, shared_action, shared_reason = _is_shared_resource(res)
            if is_shared:
                category = "shared"
                reasons.append(shared_reason)
                is_aws_default = shared_reason.startswith("AWS auto-creates") or shared_reason.startswith("AWS default")
                if shared_action == "skip":
                    decision = "exclude"
                    evidence = {"rule": "service_linked_role"}
                elif is_aws_default and r_id in degree and degree[r_id] == 0:
                    # Nothing we manage depends on it - leave it out of the code entirely.
                    decision = "exclude"
                    reasons.append("no discovered resource depends on it, so it is left out of the code")
                    evidence = {"rule": "aws_default_unreferenced"}
                else:
                    decision = "reference"
                    evidence = {"rule": "aws_default_referenced" if is_aws_default else "shared_marker"}

        # 4. Orphaned: nothing references it and it references nothing - unclear ownership.
        if category is None and r_id in degree and degree[r_id] == 0:
            services = trusted_services.get(r_id)
            if services:
                reasons.append(
                    f"trusted by {', '.join(services)} but no matching resource actually assumes it"
                )
            else:
                reasons.append("no other discovered resource references this one")
            category, decision = "orphaned", "review"
            evidence = {"rule": "orphaned_in_graph"}

        # 5. IAM is high-risk in the MVP: Review unless explicitly enabled.
        if category is None and r_type == "aws_iam_role" and not MANAGE_IAM:
            category, decision = "unmanaged", "review"
            reasons.append("IAM roles are high-risk to adopt - confirm ownership before managing (TERRAAGENT_MANAGE_IAM)")
            evidence = {"rule": "iam_requires_review"}

        # 6. Default: unmanaged, discovered, supported - Manage.
        if category is None:
            category, decision = "unmanaged", "manage"
            reasons.append("newly discovered resource with no prior Terraform state")
            evidence = {"rule": "unmanaged_default"}

        classifications.append(ResourceClassification(
            resource_id=r_id,
            resource_type=str(r_type or "unknown"),
            category=category,
            reason=reasons,
            recommended_action=DECISION_TO_ACTION[decision],
            decision=decision,
            evidence=evidence,
        ))
        summary[category] = summary.get(category, 0) + 1
        decisions[decision] = decisions.get(decision, 0) + 1

    return ClassificationReport(classifications=classifications, summary=summary, decisions=decisions)
