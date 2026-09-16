"""Agent 3: Graph Agent.
Constructs NetworkX DAG of discovered resources and topological dependencies.
"""

import logging
from typing import Any, Dict

from tools.graph_builder import DependencyGraphBuilder

logger = logging.getLogger("terraagent.agents.graph_agent")


async def graph_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Generates the dependency DAG from discovered cloud resources."""
    job_id = state.get("job_id", "")
    resources = state.get("resources", [])

    logger.info(f"[{job_id}] Graph Agent: Constructing DAG for {len(resources)} resources")

    builder = DependencyGraphBuilder()
    graph_data = builder.build_graph(resources)

    completed_agents = list(state.get("completed_agents", []))
    completed_agents.append("graph_agent")

    return {
        "dependency_graph": graph_data,
        "completed_agents": completed_agents,
        "current_agent": "classification_agent",
        "progress_percentage": 45
    }
