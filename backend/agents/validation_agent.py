"""Validation Agent: Executes IaC engine fmt, init, and validate safely in isolated temp directories."""

import logging
import re
from typing import Any, Dict

from services.redis_client import redis_service
from tools.iac_engine import get_iac_engine

logger = logging.getLogger(__name__)

_DISALLOWED_COMMAND_PATTERN = re.compile(
    r'command\s*=\s*"[^"]*\b(terraform|tofu)\s+(apply|destroy|import)\b', re.IGNORECASE
)


def _assert_no_mutating_commands(tf_files: Dict[str, str]) -> None:
    for filename, content in tf_files.items():
        match = _DISALLOWED_COMMAND_PATTERN.search(content)
        if match:
            raise ValueError(
                f"Safety Violation: generated file '{filename}' contains a provisioner "
                f"command that would execute '{match.group(1)} {match.group(2)}'. Refusing to validate."
            )


async def validation_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "unknown")
    tf_files = state.get("terraform_files", {})
    binary = state.get("terraform_binary", "terraform")
    engine = get_iac_engine(binary)

    _assert_no_mutating_commands(tf_files)

    await redis_service.publish_log(
        job_id,
        f"[AGENT:validation_agent] Starting {engine.name.upper()} validation cycle (fmt -> init -backend=false -> validate)...",
        agent_name="validation_agent"
    )

    result = await engine.validate_hcl(tf_files)
    overall_passed = result["passed"]
    checks = result.get("checks", [])

    # Classify granular status: PASS, FAIL, or PARTIAL
    if overall_passed:
        val_status = "PASS"
    elif any(c.get("passed") for c in checks):
        val_status = "PARTIAL"
    else:
        val_status = "FAIL"

    # Update generation manifest if present in state
    manifest = dict(state.get("generation_manifest") or {})
    if manifest:
        manifest["validation_status"] = val_status

    await redis_service.publish_log(
        job_id,
        f"[AGENT:validation_agent] {engine.name.upper()} validation complete. Status: {val_status}",
        agent_name="validation_agent"
    )

    completed_agents = list(state.get("completed_agents", []))
    if "validation_agent" not in completed_agents:
        completed_agents.append("validation_agent")

    return {
        "validation_results": {
            "passed": overall_passed,
            "status": val_status,
            "validation_status": val_status,
            "checks": checks
        },
        "generation_manifest": manifest,
        "completed_agents": completed_agents,
        "current_agent": "policy_agent",
        "progress_percentage": 70
    }
