"""Agent 1: Intent Router Agent.
Classifies the user input request and configures the execution path.
"""

import logging
from typing import Any, Dict

logger = logging.getLogger("terraagent.agents.intent_router")


async def intent_router_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Classifies operation intent and determines target pipeline nodes."""
    job_id = state.get("job_id", "")
    operation = state.get("operation", "generate")
    resource_filters = state.get("resource_filters", [])

    is_full_generate = operation in ("generate", "scan")
    intent = {
        "operation": operation,
        "resource_types_to_scan": resource_filters,
        "mode": "full_generate" if is_full_generate else "targeted_scan",
        "explanation": (
            f"Operation '{operation}' requested for resource types {resource_filters}: "
            + ("full discovery and Terraform HCL synthesis will run."
               if is_full_generate else
               "a targeted, read-only inspection will run without full HCL synthesis.")
        ),
    }

    logger.info(f"[{job_id}] Intent classified: {operation} ({intent['mode']})")

    # Initialize agent tracking
    completed_agents = list(state.get("completed_agents", []))
    completed_agents.append("intent_router")

    return {
        "intent": intent,
        "completed_agents": completed_agents,
        "current_agent": "cloud_discovery",
        "progress_percentage": 10
    }
