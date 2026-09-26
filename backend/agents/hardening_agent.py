"""Hardening step (Delivery & Approval Agent): builds the optional Hardening
proposal - explained security fixes on top of the adoption code, never merged
into it (tools/hardening.py).

The proposed code must still pass the same deterministic invariants as a
repair (no resource removed, no ignore_changes, no scanner suppressions, no
provisioners/external data, no literal secrets) and `terraform validate`
(through the engine's runner - never apply/destroy/import). If either fails,
no hardened files ship; the changes are kept as recommendations instead.
"""

from typing import Any, Dict

from services.redis_client import redis_service
from tools.hardening import propose
from tools.hcl_invariants import check_repair_invariants
from tools.iac_engine import get_iac_engine


async def hardening_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "unknown")
    adoption = state.get("terraform_files", {}) or {}
    classifications = (state.get("classification_results") or {}).get("classifications", []) or []
    managed = [c["resource_id"] for c in classifications if c.get("decision") == "manage"]

    proposal = propose(adoption, state.get("resources", []) or [], managed, state.get("security_results") or {})
    proposal.update({"validated": None, "validation_output": "", "rejected_reason": None})

    if proposal["files"]:
        hardened = {**adoption, **proposal["files"]}
        violations = check_repair_invariants(adoption, hardened)
        if violations:
            proposal["rejected_reason"] = "invariant check failed: " + "; ".join(violations)
        else:
            engine = get_iac_engine(state.get("terraform_binary", "terraform"))
            hardened = await engine.format_hcl(hardened)
            proposal["files"] = {k: hardened[k] for k in proposal["files"]}
            result = await engine.validate_hcl(hardened)
            proposal["validated"] = bool(result.get("passed"))
            proposal["validation_output"] = "\n".join(
                str(c.get("output", ""))[:500] for c in result.get("checks", []) if not c.get("passed")
            )
            if not proposal["validated"]:
                proposal["rejected_reason"] = "terraform validate failed on the hardened code"
        if proposal["rejected_reason"]:
            proposal["files"] = {}

    n = len(proposal["changes"])
    if proposal["rejected_reason"]:
        note = f"{n} proposed change(s) not shipped: {proposal['rejected_reason']}"
    elif n:
        note = f"{n} explained change(s) in {len(proposal['files'])} file(s), validated"
    else:
        note = "no automatic fixes apply"
    await redis_service.publish_log(
        job_id,
        f"[AGENT:hardening] Hardening proposal: {note}; "
        f"{len(proposal['recommendations'])} manual recommendation(s). The adoption code is unchanged.",
        agent_name="hardening",
    )
    return {"hardening": proposal, "current_agent": "cost_agent"}
