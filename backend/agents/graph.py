"""LangGraph StateGraph definition orchestrating all 11 TerraAgent agent nodes."""

import logging
import time
from typing import Any, Callable, Dict, List, Literal, Optional, TypedDict

from langgraph.graph import END, StateGraph

logger = logging.getLogger("terraagent.graph")

from .adoption_planning_agent import adoption_planning_agent_node
from .classification_agent import classification_agent_node
from .cloud_discovery import cloud_discovery_node
from .cost_agent import cost_agent_node
from .documentation_agent import documentation_agent_node
from .drift_reconciliation_agent import drift_reconciliation_agent_node
from .graph_agent import graph_agent_node
from .intent_router import intent_router_node
from .plan_equivalence_agent import plan_equivalence_agent_node
from .policy_agent import policy_agent_node
from .repair_agent import repair_agent_node
from .terraform_composer import terraform_composer_node
from .validation_agent import validation_agent_node


class TerraAgentState(TypedDict):
    # Execution metadata & Inputs
    job_id: str
    created_at: str  # set once at job creation - must survive every incremental
                      # progress publish in _timed() below, since GET /status
                      # falls back to "now" whenever it's missing
    operation: Literal["generate", "scan", "explain", "validate"]
    region: str
    resource_filters: List[str]
    aws_credentials: dict  # Secret values (Never printed/logged)
    aws_endpoint_url: Optional[str]  # LocalStack override for integration tests only
    role_arn: Optional[str]  # Optional STS AssumeRole target for multi-account scans
    webhook_url: Optional[str]  # Optional URL to POST a result summary to on completion
    zip_password: Optional[str]  # Optional password to AES-256 encrypt the output ZIP with
    terraform_binary: str  # "terraform" | "tofu" - which CLI TerraformRunner shells out to
    run_plan_equivalence: bool  # opt-in: run a real `terraform plan` against live AWS creds

    # Agent Outputs
    intent: dict
    resources: List[dict]
    classification_results: dict  # ClassificationReport-shaped: managed/unmanaged/shared/orphaned/unsupported
    dependency_graph: dict
    adoption_plan: dict  # AdoptionPlan-shaped: safe_to_import/review_required/do_not_manage/use_data_source/unsupported
    terraform_files: dict
    generation_manifest: dict  # GenerationManifest-shaped: job metadata, engine, resource tallies, warnings
    validation_results: dict
    drift_results: dict  # {skipped|resources_checked|resources_missing|findings|...} from drift_reconciliation_agent
    plan_equivalence_results: dict  # PlanEquivalenceResult-shaped, only populated if run_plan_equivalence
    security_results: dict
    cost_results: dict  # Infracost breakdown: total_monthly_cost/currency/resources/tool_skipped
    repair_attempts: int
    repair_risk_tier: Optional[str]  # last repair cycle's tier: "safe_auto"|"behavior_changing"|"destructive"
    pending_approval: Optional[dict]  # non-empty when repair or plan-equivalence needs a human decision
    approval_decision: Optional[dict]  # {decision, reason, decided_at} - set by POST /scan/{id}/approve|reject
    documentation: dict
    github_pr: Optional[dict]  # {pr_url, branch, ...} whole-job PR - set only via the separate /pull-request endpoint
    github_wave_prs: Dict[str, dict]  # wave number (str) -> {pr_url, branch, ...} - same endpoint, ?wave= set
    zip_path: Optional[str]
    zip_sha256: Optional[str]
    zip_manifest: List[dict]
    errors: List[str]
    status: str

    # Progress tracking (read by /scan/{job_id}/status)
    completed_agents: List[str]
    current_agent: str
    progress_percentage: int
    agent_timings: Dict[str, float]  # agent name -> duration in seconds


def should_repair(state: TerraAgentState) -> str:
    """Evaluates if security or validation failures necessitate a repair cycle."""
    sec_res = state.get("security_results", {})
    val_res = state.get("validation_results", {})
    attempts = state.get("repair_attempts", 0)

    # A "system" check (tools/terraform_runner.py::validate_hcl's exception branch -
    # e.g. the terraform binary is missing, or sandbox creation failed) is an
    # environment failure, not a content problem. No HCL patch repair_agent could
    # ever produce fixes it, so never spend a repair cycle - including a real LLM
    # call, if security findings also exist - retrying something structurally
    # unfixable. Go straight to cost estimation instead.
    if any(c.get("check_name") == "system" for c in val_res.get("checks", [])):
        return "cost_agent"

    # If critical/high security issues or validation failed, and under retry threshold
    needs_fix = (
        not val_res.get("passed", True) or
        sec_res.get("critical_count", 0) > 0 or
        sec_res.get("high_count", 0) > 0
    )

    if needs_fix and attempts < 2:
        return "repair_agent"
    return "cost_agent"


def drift_gate(state: TerraAgentState) -> str:
    """Route straight to a halt when drift_reconciliation_agent found a
    live-vs-generated attribute mismatch on a ForceNew (or otherwise
    behavior-changing) attribute - the only place a replace/destroy-
    equivalent finding comes from in this pipeline (plan_equivalence_agent
    is create-only by design; see that module's docstring). No reason to
    spend a real terraform plan or a repair cycle on HCL that's already
    known not to match reality."""
    if state.get("pending_approval"):
        return "halt"
    return "plan_equivalence_agent"


def plan_gate(state: TerraAgentState) -> str:
    """Route straight to a halt when plan_equivalence_agent found a
    replace/destroy action against already-discovered infrastructure -
    generating security/repair output against a plan that doesn't even
    match reality is wasted work, and this is exactly the kind of finding
    that must reach a human before anything else happens."""
    if state.get("pending_approval"):
        return "halt"
    return "policy_agent"


def repair_or_done(state: TerraAgentState) -> str:
    """After repair, re-validate, halt for approval, or proceed to cost estimation."""
    if state.get("pending_approval"):
        # Something (repair_agent's own tier escalation, or a pending_approval
        # already set by plan_equivalence_agent earlier in this same run) left
        # a behavior_changing/destructive finding unresolved. This must now
        # genuinely halt the pipeline rather than just noting it in the final
        # README - re-validating would just rediscover the exact same
        # findings, and continuing to cost_agent/documentation_agent would
        # ship a "COMPLETE" bundle nobody has actually approved.
        return "halt"
    attempts = state.get("repair_attempts", 0)
    if attempts <= 2:
        return "validation_agent"
    return "cost_agent"


def _timed(name: str, node_func: Callable) -> Callable:
    """Wrap an agent node to record its wall-clock duration into agent_timings,
    without requiring every individual agent file to instrument itself. Also
    the one place that reliably knows which agent was active if a node
    raises, so it records the scan_errors_total metric right here rather
    than guessing after the fact from a Celery task that has no visibility
    into how far the pipeline got.

    Also the one place that publishes live progress to Redis after every
    node - GET /api/scan/{id}/status previously only ever reflected the
    state written at job creation and at final completion/failure (verified
    live: a real ~2 minute scan showed the identical "5%, intent_router,
    RUNNING" snapshot the entire time). {**state, **result} approximates the
    full accumulated state immediately after this node - the same thing the
    next node will actually receive as its own input state - which is why
    it's the right thing to publish, not just this node's own partial
    return dict."""
    async def wrapper(state: Dict[str, Any]) -> Dict[str, Any]:
        start = time.monotonic()
        try:
            result = await node_func(state)
        except Exception:
            from routers.metrics import record_scan_error
            record_scan_error(name)
            raise
        elapsed = round(time.monotonic() - start, 3)
        timings = dict(state.get("agent_timings", {}) or {})
        timings[name] = timings.get(name, 0) + elapsed  # accumulate across repair-loop re-entries
        result["agent_timings"] = timings

        job_id = state.get("job_id")
        if job_id:
            try:
                from services.redis_client import redis_service
                await redis_service.set_job_state(job_id, {**state, **result})
            except Exception as e:
                # Best-effort instrumentation - never let a progress-publish
                # failure break the actual pipeline run.
                logger.warning(f"[{job_id}] Failed to publish live progress after {name}: {e}")

        return result
    return wrapper


def build_graph():
    """Compiles the 13-node LangGraph workflow."""
    workflow = StateGraph(TerraAgentState)

    # Register all 13 agent nodes (each wrapped to record per-agent timing)
    workflow.add_node("intent_router", _timed("intent_router", intent_router_node))
    workflow.add_node("cloud_discovery", _timed("cloud_discovery", cloud_discovery_node))
    workflow.add_node("graph_agent", _timed("graph_agent", graph_agent_node))
    workflow.add_node("classification_agent", _timed("classification_agent", classification_agent_node))
    workflow.add_node("adoption_planning_agent", _timed("adoption_planning_agent", adoption_planning_agent_node))
    workflow.add_node("terraform_composer", _timed("terraform_composer", terraform_composer_node))
    workflow.add_node("validation_agent", _timed("validation_agent", validation_agent_node))
    workflow.add_node("drift_reconciliation_agent", _timed("drift_reconciliation_agent", drift_reconciliation_agent_node))
    workflow.add_node("plan_equivalence_agent", _timed("plan_equivalence_agent", plan_equivalence_agent_node))
    workflow.add_node("policy_agent", _timed("policy_agent", policy_agent_node))
    workflow.add_node("repair_agent", _timed("repair_agent", repair_agent_node))
    workflow.add_node("cost_agent", _timed("cost_agent", cost_agent_node))
    workflow.add_node("documentation_agent", _timed("documentation_agent", documentation_agent_node))

    # Entrypoint & Linear edges
    workflow.set_entry_point("intent_router")
    workflow.add_edge("intent_router", "cloud_discovery")
    workflow.add_edge("cloud_discovery", "graph_agent")
    workflow.add_edge("graph_agent", "classification_agent")
    workflow.add_edge("classification_agent", "adoption_planning_agent")
    workflow.add_edge("adoption_planning_agent", "terraform_composer")
    workflow.add_edge("terraform_composer", "validation_agent")
    workflow.add_edge("validation_agent", "drift_reconciliation_agent")

    # Conditional branching
    workflow.add_conditional_edges(
        "drift_reconciliation_agent",
        drift_gate,
        {
            "plan_equivalence_agent": "plan_equivalence_agent",
            "halt": END
        }
    )

    workflow.add_conditional_edges(
        "plan_equivalence_agent",
        plan_gate,
        {
            "policy_agent": "policy_agent",
            "halt": END
        }
    )

    workflow.add_conditional_edges(
        "policy_agent",
        should_repair,
        {
            "repair_agent": "repair_agent",
            "cost_agent": "cost_agent"
        }
    )

    workflow.add_conditional_edges(
        "repair_agent",
        repair_or_done,
        {
            "validation_agent": "validation_agent",
            "cost_agent": "cost_agent",
            "halt": END
        }
    )

    # Cost estimation always runs against the final HCL - after the repair
    # loop concludes, right before documentation_agent packages the ZIP.
    workflow.add_edge("cost_agent", "documentation_agent")
    workflow.add_edge("documentation_agent", END)

    return workflow.compile()
