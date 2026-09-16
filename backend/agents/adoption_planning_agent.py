"""Adoption Planning Agent: turns the Classification Agent's per-resource
report into a reviewer-facing migration plan - which resources are safe to
import, which need a data source, which should never be managed, which need
manual review, plus an interpretable risk score and a dependency-safe import
order. The numeric core (models.adoption.build_adoption_plan) is pure
deterministic Python; the only LLM involvement is the plan's prose summary,
which always falls back to a deterministic template on failure (same pattern
as terraform_composer.py::_compose_via_llm and
documentation_agent.py::_generate_readme).

Runs after classification_agent, before terraform_composer:
classification_agent already needs dependency_graph (for orphan detection),
so this agent - which also needs dependency_graph, for import_order - simply
reuses whatever classification_agent already had available.
"""

import logging
from typing import Any, Dict

from services.ollama_client import ollama_client
from services.redis_client import redis_service
from tools.adoption_planner import build_adoption_plan, deterministic_summary

logger = logging.getLogger(__name__)


async def _summarize_via_llm(plan_summary_input: str) -> str:
    try:
        summary = await ollama_client.generate(
            f"Write a short (2-4 sentence) plain-English summary of this Terraform adoption plan "
            f"for a human reviewer, in prose, no markdown headers or bullet lists:\n\n{plan_summary_input}",
            system=(
                "Output plain prose only - no markdown fences, no bullet points, no headers. "
                "Do not invent numbers not present in the input; only rephrase the given counts."
            ),
            num_predict=300
        )
        summary = summary.replace("```", "").strip()
        if summary:
            return summary
    except Exception as e:
        logger.warning(f"LLM adoption plan summary failed, using deterministic fallback: {e}")
    return ""


async def adoption_planning_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "unknown")
    classification_results = state.get("classification_results", {})
    dependency_graph = state.get("dependency_graph", {})
    resources = state.get("resources", [])

    await redis_service.publish_log(
        job_id,
        "[AGENT:adoption_planning_agent] Building adoption plan from classification results...",
        agent_name="adoption_planning_agent"
    )

    plan = build_adoption_plan(classification_results, dependency_graph, resources)
    fallback_summary = deterministic_summary(plan)
    plan.summary = await _summarize_via_llm(fallback_summary) or fallback_summary

    await redis_service.publish_log(
        job_id,
        f"[AGENT:adoption_planning_agent] Adoption plan complete: risk score {plan.risk_score}/100, "
        f"{plan.total_resource_count} resource(s) planned.",
        agent_name="adoption_planning_agent"
    )

    completed_agents = list(state.get("completed_agents", []))
    if "adoption_planning_agent" not in completed_agents:
        completed_agents.append("adoption_planning_agent")

    return {
        "adoption_plan": plan.model_dump(),
        "completed_agents": completed_agents,
        "current_agent": "terraform_composer",
        "progress_percentage": 55
    }
