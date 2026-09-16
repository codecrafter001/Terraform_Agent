"""Resource Classification Agent: classifies every discovered resource as
managed / unmanaged / drifted / orphaned / shared / unsupported before any
Terraform is generated. Pure deterministic Python (tools/resource_classifier.py)
- no LLM call, no new AWS API calls.

Runs after graph_agent, not directly after cloud_discovery: the "orphaned"
rule needs dependency_graph.links/nodes (tools/graph_builder.py's output) to
know a resource has zero edges, and that graph doesn't exist until
graph_agent has run. Pipeline order is therefore
cloud_discovery -> graph_agent -> classification_agent -> adoption_planning_agent
-> terraform_composer.
"""

from typing import Any, Dict

from services.redis_client import redis_service
from tools.resource_classifier import classify_resources


async def classification_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "unknown")
    resources = state.get("resources", [])
    dependency_graph = state.get("dependency_graph", {})

    await redis_service.publish_log(
        job_id,
        f"[AGENT:classification_agent] Classifying {len(resources)} discovered resource(s)...",
        agent_name="classification_agent"
    )

    report = classify_resources(resources, dependency_graph)

    if report.summary.get("unsupported", 0) > 0:
        unsupported_ids = [c.resource_id for c in report.classifications if c.category == "unsupported"]
        await redis_service.publish_log(
            job_id,
            f"[AGENT:classification_agent] Explicitly identified {len(unsupported_ids)} unsupported resource(s): {', '.join(unsupported_ids)} - will not be synthesized into Terraform.",
            agent_name="classification_agent"
        )

    await redis_service.publish_log(
        job_id,
        f"[AGENT:classification_agent] Classification complete: {report.summary}",
        agent_name="classification_agent"
    )

    completed_agents = list(state.get("completed_agents", []))
    completed_agents.append("classification_agent")

    return {
        "classification_results": report.model_dump(),
        "completed_agents": completed_agents,
        "current_agent": "adoption_planning_agent",
        "progress_percentage": 50
    }
