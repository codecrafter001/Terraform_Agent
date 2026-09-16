"""Cost Agent: estimates the monthly cost of the final generated Terraform/
OpenTofu configuration via Infracost, before documentation_agent packages
the ZIP. Runs after the repair loop concludes (not on an earlier draft) so
the reported cost reflects exactly what ships - never mid-repair HCL that
might still change.
"""

from typing import Any, Dict

from services.redis_client import redis_service
from tools.infracost_runner import InfracostRunner


async def cost_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "unknown")
    tf_files = state.get("terraform_files", {})

    await redis_service.publish_log(
        job_id,
        "[AGENT:cost_agent] Estimating monthly cost of generated infrastructure via Infracost...",
        agent_name="cost_agent"
    )

    result = await InfracostRunner.estimate_cost(tf_files)

    if result["tool_skipped"]:
        await redis_service.publish_log(
            job_id,
            "[AGENT:cost_agent] WARNING: Infracost unavailable (missing binary or "
            "INFRACOST_API_KEY) - cost was NOT estimated, this is not a $0 result.",
            agent_name="cost_agent"
        )
    else:
        await redis_service.publish_log(
            job_id,
            f"[AGENT:cost_agent] Cost estimation complete: "
            f"${result['total_monthly_cost']:.2f}/{result['currency']} per month "
            f"across {len(result['resources'])} priced resource(s).",
            agent_name="cost_agent"
        )

    completed_agents = list(state.get("completed_agents", []))
    if "cost_agent" not in completed_agents:
        completed_agents.append("cost_agent")

    return {
        "cost_results": result,
        "completed_agents": completed_agents,
        "current_agent": "documentation_agent",
        "progress_percentage": 92
    }
