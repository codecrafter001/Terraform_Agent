"""Policy Agent: Multi-layer security scans (tfsec, Checkov, Trivy, Conftest OPA)."""

import asyncio
import logging
from typing import Any, Dict

from services.redis_client import redis_service
from tools.checkov_runner import CheckovRunner
from tools.conftest_runner import ConftestRunner
from tools.tfsec_runner import TfsecRunner
from tools.trivy_runner import TrivyRunner

logger = logging.getLogger(__name__)


async def policy_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "unknown")
    tf_files = state.get("terraform_files", {})

    await redis_service.publish_log(
        job_id,
        "[AGENT:policy_agent] Executing multi-layer security scans (tfsec + Checkov + Trivy + OPA)...",
        agent_name="policy_agent"
    )

    # Run security tools concurrently (each manages its own sandboxed temp dir)
    tfsec_res, checkov_res, trivy_res, conftest_res = await asyncio.gather(
        TfsecRunner.scan_hcl(tf_files),
        CheckovRunner.scan_hcl(tf_files),
        TrivyRunner.scan_hcl(tf_files),
        ConftestRunner.scan_hcl(tf_files)
    )

    all_findings = (
        tfsec_res.get("findings", []) +
        checkov_res.get("findings", []) +
        trivy_res.get("findings", []) +
        conftest_res.get("findings", [])
    )

    # Each runner reports tool_skipped=True when its binary wasn't found on PATH
    # (a FileNotFoundError, not "ran and found nothing"). Without surfacing this,
    # a scan on a box missing all 4 scanner binaries reports the exact same
    # "passed: true, risk_score: 0" as a genuinely clean scan - a false assurance
    # for a tool whose whole value proposition is safety validation.
    tool_results = {"tfsec": tfsec_res, "checkov": checkov_res, "trivy": trivy_res, "conftest": conftest_res}
    scanners_skipped = [name for name, res in tool_results.items() if res.get("tool_skipped")]

    critical_count = sum(1 for f in all_findings if f.get("severity") == "CRITICAL")
    high_count = sum(1 for f in all_findings if f.get("severity") == "HIGH")
    medium_count = sum(1 for f in all_findings if f.get("severity") == "MEDIUM")
    low_count = sum(1 for f in all_findings if f.get("severity") == "LOW")

    # Risk score calculation: 0 (clean) to 100 (critical risk)
    risk_score = min(100, (critical_count * 30) + (high_count * 15) + (medium_count * 5) + (low_count * 1))
    passed = (critical_count == 0 and high_count == 0)

    if scanners_skipped:
        await redis_service.publish_log(
            job_id,
            f"[AGENT:policy_agent] WARNING: {', '.join(scanners_skipped)} not found on PATH - "
            f"skipped entirely, not scanned clean. Risk score below does not reflect these tools.",
            agent_name="policy_agent"
        )

    await redis_service.publish_log(
        job_id,
        f"[AGENT:policy_agent] Security scan complete: Risk Score = {risk_score}/100, Findings: {len(all_findings)} (Critical: {critical_count}, High: {high_count})",
        agent_name="policy_agent"
    )

    completed_agents = list(state.get("completed_agents", []))
    if "policy_agent" not in completed_agents:
        completed_agents.append("policy_agent")

    return {
        "security_results": {
            "passed": passed,
            "risk_score": risk_score,
            "critical_count": critical_count,
            "high_count": high_count,
            "medium_count": medium_count,
            "low_count": low_count,
            "findings": all_findings,
            "scanners_skipped": scanners_skipped,
            "compliance_summary": {
                "checkov": {
                    "passed": checkov_res.get("passed", 0),
                    "failed": checkov_res.get("failed", 0),
                    "skipped": checkov_res.get("skipped", 0)
                }
            }
        },
        "completed_agents": completed_agents,
        "current_agent": "cost_agent",
        "progress_percentage": 85
    }

