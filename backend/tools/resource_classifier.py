"""Deterministic resource classification - no LLM, no new external calls.

Classifies every discovered resource as managed / unmanaged / drifted /
orphaned / shared / unsupported, per the decision-tree design in
docs/design/cloud-inventory-and-adoption-planning.md. This is the agent
that makes the eventual Adoption Plan (and the composer's skip/data-source
branches) possible - everything here only reads fields already present in
discovered resource dicts (tools/aws_scanner.py) and the dependency graph
(tools/graph_builder.py); it adds no new AWS API calls.
"""

from typing import Any, Dict, List, Tuple

from models.adoption import ClassificationReport, ResourceClassification

# Exactly the 8 resource types AWSScanner (tools/aws_scanner.py) discovers today.
# Kept as an explicit whitelist rather than an implicit "not vpc/subnet/..." check
# so this degrades safely (falls into "unsupported", never crashes) the day
# discovery adds a 9th type before this classifier is updated to match.
DISCOVERY_RESOURCE_TYPES = {
    "aws_vpc", "aws_subnet", "aws_route_table", "aws_security_group",
    "aws_instance", "aws_s3_bucket", "aws_db_instance", "aws_iam_role",
}


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
    """Classifies every resource into one of:
    - managed: already managed by Terraform/IaC (skip generation)
    - shared: AWS default/service-linked or shared resource (data_source or skip)
    - orphaned: no incoming/outgoing dependency links in graph (manual_review)
    - unsupported: unknown resource type or malformed data (manual_review)
    - unmanaged: newly discovered active resource to be imported (import)
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

    if not isinstance(resources, list):
        return ClassificationReport(classifications=[], summary=summary)

    for idx, res in enumerate(resources):
        if not isinstance(res, dict):
            classifications.append(ResourceClassification(
                resource_id=f"malformed_{idx}",
                resource_type="unknown",
                category="unsupported",
                reason=["malformed discovery data: resource is not a dictionary"],
                recommended_action="manual_review"
            ))
            summary["unsupported"] += 1
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
                recommended_action="manual_review"
            ))
            summary["unsupported"] += 1
            continue

        r_id = str(r_id)
        reasons: List[str] = []
        category = None
        action = None

        # 1. Unsupported check: missing or unsupported resource type
        if not r_type or r_type not in DISCOVERY_RESOURCE_TYPES:
            category, action = "unsupported", "manual_review"
            reasons.append(f"resource type '{r_type or 'unknown'}' has no adoption support yet")

        # 2. Managed check: already managed by Terraform/IaC
        elif _is_managed_by_terraform(res):
            category, action = "managed", "skip"
            reasons.append("resource is already managed by Terraform/IaC (detected via tag/metadata)")

        # 3. Shared check: default VPC, default SG, service-linked IAM role, or shared tag
        else:
            is_shared, shared_action, shared_reason = _is_shared_resource(res)
            if is_shared:
                category, action = "shared", shared_action
                reasons.append(shared_reason)

        # 4. Orphaned check: zero degree in dependency graph (or IAM role with unused trust policy)
        if category is None and r_id in degree and degree[r_id] == 0:
            services = trusted_services.get(r_id)
            if services:
                reasons.append(
                    f"trusted by {', '.join(services)} but no matching resource actually assumes it"
                )
            else:
                reasons.append("no other discovered resource references this one")
            category, action = "orphaned", "manual_review"

        # 5. Default: Unmanaged newly discovered resource
        if category is None:
            category, action = "unmanaged", "import"
            reasons.append("newly discovered resource with no prior Terraform state")

        classifications.append(ResourceClassification(
            resource_id=r_id,
            resource_type=str(r_type or "unknown"),
            category=category,
            reason=reasons,
            recommended_action=action
        ))
        summary[category] = summary.get(category, 0) + 1

    return ClassificationReport(classifications=classifications, summary=summary)
