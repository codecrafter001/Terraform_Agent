"""Agent 1: Intent Router Agent.
Classifies the user input request and configures the execution path.
"""

import logging
from typing import Any, Dict

from tools.intent_analyzer import analyze_user_intent

logger = logging.getLogger("terraagent.agents.intent_router")


async def intent_router_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Classifies operation intent and determines target pipeline nodes."""
    job_id = state.get("job_id", "")
    operation = state.get("operation", "generate")
    resource_filters = state.get("resource_filters", [])
    user_request = state.get("user_request", "")
    environment = state.get("environment", "production")
    region = state.get("region", "us-east-1")
    analyzed_intent = state.get("analyzed_intent")

    # If analyzed_intent was provided from pre-scan confirmation, use it; otherwise analyze
    if analyzed_intent and isinstance(analyzed_intent, dict) and "operation" in analyzed_intent:
        intent_data = analyzed_intent
    elif user_request and user_request.strip():
        intent_data = await analyze_user_intent(
            user_request=user_request,
            region=region,
            environment=environment,
            resource_filters=resource_filters,
        )
    else:
        is_full_generate = operation in ("generate", "scan", "modify", "fix")
        intent_data = {
            "operation": operation,
            "operation_label": operation.title(),
            "target_resources": [],
            "requested_changes": [],
            "confidence_score": 1.0,
            "resource_types_to_scan": resource_filters,
            "mode": "full_generate" if is_full_generate else "targeted_scan",
            "explanation": (
                f"Operation '{operation}' requested for resource types {resource_filters}: "
                + ("full discovery and Terraform HCL synthesis will run."
                   if is_full_generate else
                   "a targeted, read-only inspection will run without full HCL synthesis.")
            ),
            "environment": environment,
            "region": region,
            "risk_level": "low",
        }

    # Ensure standard fields
    is_full_generate = intent_data.get("operation", operation) in ("generate", "scan", "modify", "fix")
    intent = {
        "operation": intent_data.get("operation", operation),
        "operation_label": intent_data.get("operation_label", operation.title()),
        "target_resources": intent_data.get("target_resources", []),
        "requested_changes": intent_data.get("requested_changes", []),
        "user_request": user_request,
        "environment": environment,
        "region": region,
        "risk_level": intent_data.get("risk_level", "low"),
        "confidence_score": intent_data.get("confidence_score", 0.95),
        "summary": intent_data.get("summary", ""),
        "resource_types_to_scan": resource_filters,
        "mode": "full_generate" if is_full_generate else "targeted_scan",
        "explanation": intent_data.get("summary") or (
            f"Operation '{intent_data.get('operation', operation)}' configured for {environment} ({region})."
        ),
    }

    logger.info(f"[{job_id}] Intent classified: {intent['operation']} ({intent['mode']}) - {len(intent['target_resources'])} target resource(s)")

    # Initialize agent tracking
    completed_agents = list(state.get("completed_agents", []))
    if "intent_router" not in completed_agents:
        completed_agents.append("intent_router")

    return {
        "intent": intent,
        "completed_agents": completed_agents,
        "current_agent": "resource_explorer",
        "progress_percentage": 10
    }
