"""Drift Reconciliation Agent: diffs each adopted resource's live AWS
attributes against what terraform_composer.py actually wrote into the
generated HCL for it - the only place a replace/destroy-*equivalent*
finding comes from in this pipeline (see
docs/design/phase0-plan-drift-cost-and-confidence.md Part A for the full
rationale). plan_equivalence_agent proves the generated HCL is *accepted*
by AWS (create-only, by design - see that module's docstring); this agent
proves it *matches reality*.

No terraform binary, no state, no plan - pure AWS API + generated-HCL-input
comparison. For the deterministic-template resource types
(tools/aws_live_fetch.py::SUPPORTED_RESOURCE_TYPES), terraform_composer.py's
"input" for a resource IS the discovered resource dict itself (`config =
res` in that file - no separate snapshot object exists), read via
`config.get(field, default)` calls with hardcoded defaults. So the highest-
value finding here isn't really "AWS changed in the last few minutes" (a
narrow race window) - it's "the discovered resource was missing a field, so
the composer silently substituted a placeholder default into the HCL",
which for a ForceNew attribute (e.g. aws_vpc.cidr_block) is exactly the kind
of thing that would make a real import fail or force an unwanted replace.

Resource types composed via the LLM fallback path (anything without a
deterministic template) are NOT covered in this version - verifying what
the LLM actually wrote requires parsing the generated HCL text itself, a
different and harder problem than diffing against a known input snapshot.
Deliberate, documented scope limit, not an oversight.
"""

from typing import Any, Dict, List, Optional, Tuple

from services.redis_client import redis_service
from tools.aws_live_fetch import SUPPORTED_RESOURCE_TYPES, build_session, fetch_live_resource

from tools.naming import unique_clean_name

_TIER_RANK: Dict[str, int] = {"safe_auto": 0, "behavior_changing": 1, "destructive": 2}


def _max_tier(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if a is None:
        return b
    if b is None:
        return a
    return a if _TIER_RANK.get(a, -1) >= _TIER_RANK.get(b, -1) else b


# {resource_type: {hcl_attribute: (discovered_field, composer_default, is_force_new)}}
# is_force_new = True means this AWS provider attribute cannot be changed
# in place - Terraform would have to replace the whole resource (destructive_equivalent).
_FIELD_MAP: Dict[str, Dict[str, Tuple[str, Any, bool]]] = {
    "aws_vpc": {
        "cidr_block": ("cidr_block", "10.0.0.0/16", True),
    },
    "aws_subnet": {
        "vpc_id": ("vpc_id", "", True),
        "cidr_block": ("cidr_block", "10.0.1.0/24", True),
        "availability_zone": ("availability_zone", None, True),
    },
    "aws_security_group": {
        "vpc_id": ("vpc_id", "", True),
        "description": ("description", "Managed by TerraAgent", False),
    },
    "aws_instance": {
        "ami": ("ami", "ami-0c55b159cbfafe1f0", True),
        "instance_type": ("instance_type", "t3.micro", False),
        "subnet_id": ("subnet_id", "", True),
    },
    "aws_route_table": {
        "vpc_id": ("vpc_id", "", True),
    },
    "aws_s3_bucket": {
        "bucket": ("name", "", True),
    },
}


def _composer_wrote(discovered: Dict[str, Any], field: str, default: Any) -> Any:
    return discovered.get(field, default)


def _diff_scalar_fields(
    resource_type: str, resource_id: str, discovered: Dict[str, Any], live: Dict[str, Any]
) -> List[Dict[str, Any]]:
    findings = []
    clean_name = unique_clean_name(discovered.get("name", resource_id), resource_id)
    tf_address = f"{resource_type}.{clean_name}"

    for hcl_attr, (field, default, force_new) in _FIELD_MAP.get(resource_type, {}).items():
        generated_value = _composer_wrote(discovered, field, default)
        live_value = live.get(field)

        # Normalize empty vs None
        norm_gen = "" if generated_value is None else str(generated_value).strip()
        norm_live = "" if live_value is None else str(live_value).strip()

        if norm_gen and norm_live and norm_gen != norm_live:
            tier = "destructive_equivalent" if force_new else "behavior_changing"
            impact = "replacement" if force_new else "configuration_mismatch"
            reason = (
                f"Attribute '{hcl_attr}' is ForceNew (cannot be changed in place). Mismatch forces resource recreation."
                if force_new
                else f"Attribute '{hcl_attr}' differs, which changes resource configuration at runtime."
            )
            evidence = [
                f"Live AWS attribute '{field}' = '{live_value}'",
                f"Generated HCL '{hcl_attr}' = '{generated_value}'",
                f"Terraform address: {tf_address}"
            ]
            findings.append({
                "resource": tf_address,
                "resource_id": resource_id,
                "resource_type": resource_type,
                "terraform_address": tf_address,
                "attribute": hcl_attr,
                "generated_value": generated_value,
                "live_value": live_value,
                "tier": tier,
                "impact": impact,
                "reason": reason,
                "evidence": evidence,
                "description": (
                    f"`{hcl_attr}` in the generated HCL is `{generated_value}`, but the live "
                    f"resource's actual value is `{live_value}`."
                ),
            })
    return findings


def _diff_tags(
    resource_type: str, resource_id: str, discovered: Dict[str, Any], live: Dict[str, Any]
) -> List[Dict[str, Any]]:
    discovered_tags = {t.get("Key"): t.get("Value") for t in (discovered.get("tags") or [])}
    live_tags = {t.get("Key"): t.get("Value") for t in (live.get("tags") or [])}
    if discovered_tags == live_tags:
        return []

    clean_name = unique_clean_name(discovered.get("name", resource_id), resource_id)
    tf_address = f"{resource_type}.{clean_name}"
    return [{
        "resource": tf_address,
        "resource_id": resource_id,
        "resource_type": resource_type,
        "terraform_address": tf_address,
        "attribute": "tags",
        "generated_value": discovered_tags,
        "live_value": live_tags,
        "tier": "informational",
        "impact": "cosmetic_metadata",
        "reason": "Tags differ between the generated configuration and live AWS.",
        "evidence": [f"Live tags: {live_tags}", f"Generated tags: {discovered_tags}"],
        "description": "Tags differ between the generated configuration and the live resource - cosmetic, never blocks.",
    }]


def _normalize_permission(perm: Dict[str, Any]) -> Tuple[Any, Any, Any, Tuple[str, ...]]:
    cidrs = tuple(sorted(c.get("CidrIp") for c in perm.get("IpRanges", []) if c.get("CidrIp")))
    return (perm.get("IpProtocol"), perm.get("FromPort"), perm.get("ToPort"), cidrs)


def _diff_security_group_rules(
    resource_id: str, discovered: Dict[str, Any], live: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Rule-level diff, comparing protocol, ports, CIDRs, and direction."""
    findings = []
    clean_name = unique_clean_name(discovered.get("name", resource_id), resource_id)
    tf_address = f"aws_security_group.{clean_name}"

    for direction, key in (("ingress", "ip_permissions"), ("egress", "ip_permissions_egress")):
        discovered_rules = {_normalize_permission(p) for p in (discovered.get(key) or [])}
        live_rules = {_normalize_permission(p) for p in (live.get(key) or [])}

        for rule in live_rules - discovered_rules:
            findings.append({
                "resource": tf_address,
                "resource_id": resource_id,
                "resource_type": "aws_security_group",
                "terraform_address": tf_address,
                "attribute": f"{direction} rule {rule}",
                "generated_value": None,
                "live_value": rule,
                "tier": "behavior_changing",
                "impact": "missing_rule_in_iac",
                "reason": f"Live AWS security group has an active {direction} rule not defined in the generated HCL.",
                "evidence": [f"Live {direction} rule present: {rule}", "Generated HCL omits this rule"],
                "description": f"Live security group has a {direction} rule not present in the generated HCL: {rule}.",
            })
        for rule in discovered_rules - live_rules:
            findings.append({
                "resource": tf_address,
                "resource_id": resource_id,
                "resource_type": "aws_security_group",
                "terraform_address": tf_address,
                "attribute": f"{direction} rule {rule}",
                "generated_value": rule,
                "live_value": None,
                "tier": "behavior_changing",
                "impact": "extra_rule_in_iac",
                "reason": f"Generated HCL defines a {direction} rule no longer active on live AWS.",
                "evidence": [f"Generated {direction} rule present: {rule}", "Live AWS omits this rule"],
                "description": f"Generated HCL has a {direction} rule no longer present on the live resource: {rule}.",
            })
    return findings


def _existence_finding(resource_type: str, resource_id: str, name: Optional[str] = None) -> Dict[str, Any]:
    clean_name = unique_clean_name(name or resource_id, resource_id)
    tf_address = f"{resource_type}.{clean_name}"
    return {
        "resource": tf_address,
        "resource_id": resource_id,
        "resource_type": resource_type,
        "terraform_address": tf_address,
        "attribute": "(entire resource)",
        "generated_value": "present in generated HCL",
        "live_value": "no longer exists in AWS",
        "tier": "destructive_equivalent",
        "impact": "deleted_resource",
        "reason": "Resource was discovered earlier but no longer exists in AWS live environment.",
        "evidence": [f"Resource ID '{resource_id}' not returned by live AWS Describe API"],
        "description": (
            "This resource was discovered earlier in this scan but no longer exists in AWS - "
            "importing it would fail outright."
        ),
    }


def _diff_resource(
    resource_type: str, resource_id: str, discovered: Dict[str, Any], live: Dict[str, Any]
) -> List[Dict[str, Any]]:
    findings = _diff_scalar_fields(resource_type, resource_id, discovered, live)
    findings += _diff_tags(resource_type, resource_id, discovered, live)
    if resource_type == "aws_security_group":
        findings += _diff_security_group_rules(resource_id, discovered, live)
    return findings


async def drift_reconciliation_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "unknown")
    completed_agents = list(state.get("completed_agents", []))
    if "drift_reconciliation_agent" not in completed_agents:
        completed_agents.append("drift_reconciliation_agent")

    existing_pending = state.get("pending_approval")
    existing_tier = state.get("repair_risk_tier")

    classification_map = {
        c["resource_id"]: c["recommended_action"]
        for c in (state.get("classification_results") or {}).get("classifications", [])
    }
    plan_category_map = {
        rid: cat["category"]
        for cat in (state.get("adoption_plan") or {}).get("categories", [])
        for rid in cat.get("resource_ids", [])
    }

    resources_by_id = {r["id"]: r for r in (state.get("resources") or []) if r.get("id")}
    
    # Adopted resources are those planned for import or data_source
    adopted_ids = [
        rid for rid, res in resources_by_id.items()
        if (plan_category_map.get(rid) in ("safe_to_import", "use_data_source")
            or classification_map.get(rid) in ("import", "data_source"))
        and res.get("resource_type") in SUPPORTED_RESOURCE_TYPES
    ]

    if not adopted_ids:
        return {
            "drift_results": {
                "skipped": True,
                "reason": "no adopted resources with a supported resource type to check",
                "resources_checked": 0,
                "no_drift": 0,
                "informational_count": 0,
                "behavior_changing_count": 0,
                "destructive_equivalent_count": 0,
                "resources_missing": 0,
                "resources_skipped": len(resources_by_id),
                "unable_to_verify": 0,
            },
            "pending_approval": existing_pending,
            "repair_risk_tier": existing_tier,
            "completed_agents": completed_agents,
            "current_agent": "plan_equivalence_agent",
            "progress_percentage": 63,
        }

    creds = state.get("aws_credentials") or {}
    if not creds.get("access_key") or not creds.get("secret_key"):
        await redis_service.publish_log(
            job_id,
            "[AGENT:drift_reconciliation_agent] No AWS credentials available in this pipeline run - "
            "skipping the live drift check.",
            agent_name="drift_reconciliation_agent",
        )
        return {
            "drift_results": {
                "skipped": True,
                "reason": "no AWS credentials available",
                "resources_checked": 0,
                "no_drift": 0,
                "informational_count": 0,
                "behavior_changing_count": 0,
                "destructive_equivalent_count": 0,
                "resources_missing": 0,
                "resources_skipped": 0,
                "unable_to_verify": len(adopted_ids),
            },
            "pending_approval": existing_pending,
            "repair_risk_tier": existing_tier,
            "completed_agents": completed_agents,
            "current_agent": "plan_equivalence_agent",
            "progress_percentage": 63,
        }

    await redis_service.publish_log(
        job_id,
        f"[AGENT:drift_reconciliation_agent] Re-checking {len(adopted_ids)} adopted resource(s) "
        "against live AWS...",
        agent_name="drift_reconciliation_agent",
    )

    session = build_session(creds, state.get("region", "us-east-1"))
    endpoint_url = state.get("aws_endpoint_url")

    all_findings: List[Dict[str, Any]] = []
    resources_checked = 0
    resources_missing = 0
    resources_with_drift = set()

    for rid in adopted_ids:
        discovered = resources_by_id[rid]
        r_type = discovered.get("resource_type")
        live = fetch_live_resource(session, r_type, rid, endpoint_url)
        resources_checked += 1

        if live is None:
            resources_missing += 1
            resources_with_drift.add(rid)
            all_findings.append(_existence_finding(r_type, rid, discovered.get("name")))
            continue

        res_findings = _diff_resource(r_type, rid, discovered, live)
        if res_findings:
            resources_with_drift.add(rid)
            all_findings.extend(res_findings)

    informational = [f for f in all_findings if f["tier"] == "informational"]
    blocking = [f for f in all_findings if f["tier"] != "informational"]
    no_drift_count = max(0, resources_checked - len(resources_with_drift))

    drift_results = {
        "skipped": False,
        "resources_checked": resources_checked,
        "no_drift": no_drift_count,
        "resources_missing": resources_missing,
        "findings": all_findings,
        "informational_count": len(informational),
        "behavior_changing_count": sum(1 for f in blocking if f["tier"] == "behavior_changing"),
        "destructive_equivalent_count": sum(1 for f in blocking if f["tier"] == "destructive_equivalent"),
        "resources_skipped": len(resources_by_id) - len(adopted_ids),
        "unable_to_verify": 0,
    }

    if blocking:
        new_pending_findings = [
            {
                "tool": "drift_reconciliation",
                "rule_id": "live-attribute-drift",
                "severity": "HIGH" if f["tier"] == "destructive_equivalent" else "MEDIUM",
                "description": f["description"],
                "resource": f["resource"],
                "tier": "destructive" if f["tier"] == "destructive_equivalent" else "behavior_changing",
                "reason": f.get("reason"),
                "evidence": f.get("evidence"),
                "attribute": f.get("attribute"),
                "live_value": f.get("live_value"),
                "generated_value": f.get("generated_value"),
            }
            for f in blocking
        ]
        existing_findings = (existing_pending or {}).get("findings", [])
        pending_approval = {
            "reason": "drift_reconciliation_requires_human_approval",
            "findings": existing_findings + new_pending_findings,
        }
        cycle_tier = "destructive" if any(f["tier"] == "destructive_equivalent" for f in blocking) else "behavior_changing"
        repair_risk_tier = _max_tier(existing_tier, cycle_tier)

        await redis_service.publish_log(
            job_id,
            f"[AGENT:drift_reconciliation_agent] BLOCKED: {len(blocking)} drift finding(s) "
            f"({resources_missing} resource(s) no longer exist) - halting for human approval.",
            agent_name="drift_reconciliation_agent",
        )

        return {
            "drift_results": drift_results,
            "pending_approval": pending_approval,
            "repair_risk_tier": repair_risk_tier,
            "completed_agents": completed_agents,
            "current_agent": "awaiting_approval",
            "progress_percentage": 64,
            "status": "AWAITING_APPROVAL",
        }

    await redis_service.publish_log(
        job_id,
        f"[AGENT:drift_reconciliation_agent] Drift check clean: {resources_checked} resource(s) "
        f"verified against live AWS, {len(informational)} informational (tag) difference(s).",
        agent_name="drift_reconciliation_agent",
    )

    return {
        "drift_results": drift_results,
        "pending_approval": existing_pending,
        "repair_risk_tier": existing_tier,
        "completed_agents": completed_agents,
        "current_agent": "plan_equivalence_agent",
        "progress_percentage": 63,
    }
