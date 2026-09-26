"""Cost Agent: the monthly cost delta of the optional Hardening proposal, via
Infracost.

The adoption code imports what already exists with zero changes, so it has no
cost delta - Infracost only runs when there is a hardening proposal, once on
the adoption code (baseline) and once with the hardening applied. A missing
binary or INFRACOST_API_KEY is reported as "not estimated", never as $0.
"""

from typing import Any, Dict

from services.redis_client import redis_service
from tools.infracost_runner import InfracostRunner

NO_DELTA_REASON = "The adoption code imports existing resources with no changes, so there is no cost delta."


async def cost_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "unknown")
    adoption = state.get("terraform_files", {}) or {}
    hardening = state.get("hardening") or {}
    completed_agents = list(state.get("completed_agents", []))
    if "cost_agent" not in completed_agents:
        completed_agents.append("cost_agent")

    if not hardening.get("files"):
        await redis_service.publish_log(
            job_id, f"[AGENT:cost_agent] Skipped: {NO_DELTA_REASON}", agent_name="cost_agent")
        return {
            "cost_results": {"skipped": True, "reason": NO_DELTA_REASON, "tool_skipped": False},
            "completed_agents": completed_agents,
            "current_agent": "documentation_agent",
            "progress_percentage": 92,
        }

    await redis_service.publish_log(
        job_id, "[AGENT:cost_agent] Estimating the Hardening proposal's cost delta via Infracost...",
        agent_name="cost_agent")
    baseline = await InfracostRunner.estimate_cost(adoption)
    hardened = await InfracostRunner.estimate_cost({**adoption, **hardening["files"]})
    tool_skipped = bool(baseline.get("tool_skipped") or hardened.get("tool_skipped"))
    delta = None if tool_skipped else round(
        float(hardened.get("total_monthly_cost", 0)) - float(baseline.get("total_monthly_cost", 0)), 2)
    currency = hardened.get("currency") or baseline.get("currency") or "USD"

    if tool_skipped:
        await redis_service.publish_log(
            job_id,
            "[AGENT:cost_agent] WARNING: Infracost unavailable (missing binary or INFRACOST_API_KEY) - "
            "the hardening cost delta was NOT estimated, this is not a $0 result.",
            agent_name="cost_agent")
    else:
        await redis_service.publish_log(
            job_id, f"[AGENT:cost_agent] Hardening cost delta: {delta:+.2f} {currency}/month.",
            agent_name="cost_agent")

    result = {
        "skipped": False,
        "tool_skipped": tool_skipped,
        "scope": "hardening",
        "currency": currency,
        "baseline_monthly_cost": None if tool_skipped else baseline.get("total_monthly_cost"),
        "hardened_monthly_cost": None if tool_skipped else hardened.get("total_monthly_cost"),
        "monthly_delta": delta,
        "total_monthly_cost": None if tool_skipped else hardened.get("total_monthly_cost"),
        "resources": hardened.get("resources", []),
    }
    return {
        "cost_results": result,
        "hardening": {**hardening, "cost": result},
        "completed_agents": completed_agents,
        "current_agent": "documentation_agent",
        "progress_percentage": 92,
    }
