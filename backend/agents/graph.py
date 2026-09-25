"""LangGraph StateGraph: four agents and a self-correcting verify/repair loop.

    discovery -> composer -> verifier <-> repair
                                 |
                                 +--> package -> END   (docs + cost + ZIP; not an agent)
                                 +--> END              (halt: AWAITING_APPROVAL)

Each of the four agent nodes runs an ordered set of *steps* - the original
per-concern node functions (cloud_discovery, validation_agent, policy_agent,
...), which stay individually unit-tested. Only two steps actually ask an LLM
to decide something (terraform_composer's HCL synthesis and repair_agent's
safe_auto fixes); the rest are deterministic tool calls. Grouping them this way
is what the UI shows, and it makes the Verifier <-> Repair loop - the part of
the system that actually reasons and self-corrects - the visible centerpiece.

Safety invariants carried over unchanged from the previous 13-node graph:
- A pending_approval (behavior_changing/destructive drift, plan, or repair
  finding) halts the graph at END with status AWAITING_APPROVAL. Nothing
  downstream runs until POST /scan/{id}/approve resumes the package step.
- A "system" validation check (terraform binary missing, sandbox failure) is an
  environment failure no HCL patch can fix, so it never triggers repair.
- The repair loop is bounded by max_repair_iterations.
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple, TypedDict

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
from .resource_explorer_step import resource_explorer_node
from .terraform_composer import terraform_composer_node
from .validation_agent import validation_agent_node

# Previously hardcoded as `attempts < 2` in should_repair - same default.
MAX_REPAIR_ITERATIONS = int(os.getenv("TERRAAGENT_MAX_REPAIR_ITERATIONS", "2"))

# How often a long-running step (e.g. boto3 discovery stuck in retries) emits a
# "still working" log line, so the live log never goes silent for minutes.
HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("TERRAAGENT_HEARTBEAT_SECONDS", "15"))

# Display order. "package" is the output step, not an agent.
AGENT_STAGES: Tuple[str, ...] = ("discovery", "composer", "verifier", "repair")
ALL_STAGES: Tuple[str, ...] = AGENT_STAGES + ("package",)


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
    use_resource_explorer: bool  # query AWS Resource Explorer for an all-region inventory first
    requested_region: str  # what the user asked for ("auto" or a region); "region" is what was scanned

    # Agent Outputs
    intent: dict
    resource_inventory: dict  # Resource Explorer inventory (tools/resource_explorer.py), or {available: False, reason}
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

    # Step-level progress (read by /scan/{job_id}/status) - one entry per
    # underlying step function, kept for metrics and backwards compatibility.
    completed_agents: List[str]
    current_agent: str
    progress_percentage: int
    agent_timings: Dict[str, float]  # step name -> duration in seconds

    # Agent-level progress - what the UI renders.
    current_stage: str  # one of ALL_STAGES, "awaiting_approval", or "complete"
    completed_stages: List[str]
    stage_summaries: Dict[str, str]  # stage -> one-line human summary of its latest run
    verification_iterations: List[dict]  # one entry per verifier pass (see _record_iteration)
    max_repair_iterations: int


def build_initial_state(job_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
    """Initial graph state for a new job. Shared by the Celery task and the
    inline (no-broker) runner so the two can never drift apart."""
    return {
        "job_id": job_id,
        "created_at": request.get("created_at", datetime.utcnow().isoformat()),
        "operation": request.get("operation", "generate"),
        "region": request.get("region", "us-east-1"),
        "resource_filters": request.get("resource_filters", ["EC2", "VPC", "S3", "RDS", "IAM", "SG"]),
        "aws_credentials": {
            "access_key": request.get("aws_access_key"),
            "secret_key": request.get("aws_secret_key"),
            "session_token": request.get("aws_session_token"),
        },
        "aws_endpoint_url": request.get("aws_endpoint_url"),
        "role_arn": request.get("role_arn"),
        "webhook_url": request.get("webhook_url"),
        "zip_password": request.get("zip_password"),
        "terraform_binary": request.get("terraform_binary", "terraform"),
        "run_plan_equivalence": request.get("run_plan_equivalence", False),
        "use_resource_explorer": request.get("use_resource_explorer", True),
        "requested_region": request.get("region", "us-east-1"),
        "intent": {},
        "resource_inventory": {},
        "resources": [],
        "classification_results": {},
        "dependency_graph": {},
        "adoption_plan": {},
        "terraform_files": {},
        "generation_manifest": {},
        "validation_results": {},
        "drift_results": {},
        "plan_equivalence_results": {},
        "security_results": {},
        "cost_results": {},
        "repair_attempts": 0,
        "repair_risk_tier": None,
        "pending_approval": None,
        "approval_decision": None,
        "documentation": {},
        "github_pr": None,
        "github_wave_prs": {},
        "zip_path": None,
        "zip_sha256": None,
        "zip_manifest": [],
        "errors": [],
        "status": "RUNNING",
        "completed_agents": [],
        "current_agent": "intent_router",
        "progress_percentage": 0,
        "agent_timings": {},
        "current_stage": "discovery",
        "completed_stages": [],
        "stage_summaries": {},
        "verification_iterations": [],
        "max_repair_iterations": MAX_REPAIR_ITERATIONS,
    }


# ---------------------------------------------------------------------------
# Instrumentation
# ---------------------------------------------------------------------------

def _timed(name: str, node_func: Callable) -> Callable:
    """Wrap a step to record its wall-clock duration into agent_timings,
    without requiring every individual step file to instrument itself. Also
    the one place that reliably knows which step was active if one raises,
    so it records the scan_errors_total metric right here rather than
    guessing after the fact from a Celery task that has no visibility into
    how far the pipeline got.

    Also publishes live progress to Redis after every step - GET
    /api/scan/{id}/status would otherwise only reflect the state written at
    job creation and at final completion/failure. {**state, **result}
    approximates the full accumulated state immediately after this step,
    which is why it's the right thing to publish, not just this step's own
    partial return dict."""
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
            await _publish_state(job_id, {**state, **result}, f"after {name}")

        return result
    return wrapper


async def _publish_state(job_id: str, state: Dict[str, Any], context: str) -> None:
    try:
        from services.redis_client import redis_service
        await redis_service.set_job_state(job_id, state)
    except Exception as e:
        # Best-effort instrumentation - never let a progress-publish failure
        # break the actual pipeline run.
        logger.warning(f"[{job_id}] Failed to publish live progress {context}: {e}")


async def _log(job_id: str, stage: str, message: str) -> None:
    try:
        from services.redis_client import redis_service
        await redis_service.publish_log(job_id, f"[AGENT:{stage}] {message}", agent_name=stage)
    except Exception as e:
        logger.debug(f"[{job_id}] log publish failed: {e}")


async def _with_heartbeat(job_id: str, stage: str, label: str, coro: Awaitable[Dict[str, Any]]) -> Dict[str, Any]:
    """Await a step, emitting a 'still working' log line every
    HEARTBEAT_INTERVAL_SECONDS so a slow step (boto3 retrying a timeout, a
    local LLM generating HCL) never leaves the live log silent."""
    task = asyncio.ensure_future(coro)
    waited = 0.0
    while True:
        done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_INTERVAL_SECONDS)
        if done:
            return task.result()  # re-raises the step's exception, if any
        waited += HEARTBEAT_INTERVAL_SECONDS
        await _log(job_id, stage, f"Still working: {label} ({int(waited)}s elapsed)...")


Step = Tuple[str, str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]]


async def _run_steps(
    stage: str,
    state: Dict[str, Any],
    steps: List[Step],
    stop_if: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Run a stage's steps in order. Returns (delta, accumulated_state): delta
    is what the LangGraph node returns (every key any step changed), and
    accumulated_state is the full state after the last step that ran.
    stop_if is checked after each step - used to stop a verifier pass the
    moment a step raises a pending_approval, exactly like the old
    drift_gate/plan_gate edges did."""
    job_id = state.get("job_id", "")
    acc = dict(state)
    delta: Dict[str, Any] = {}

    start = {"current_stage": stage}
    acc.update(start)
    delta.update(start)
    if job_id:
        await _publish_state(job_id, acc, f"entering {stage}")

    for step_name, label, fn in steps:
        result = await _with_heartbeat(job_id, stage, label, _timed(step_name, fn)(acc))
        acc.update(result)
        delta.update(result)
        if stop_if and stop_if(acc):
            break
    return delta, acc


def _finish_stage(stage: str, acc: Dict[str, Any], delta: Dict[str, Any], summary: str, progress: int) -> Dict[str, Any]:
    completed = list(acc.get("completed_stages", []) or [])
    if stage not in completed:
        completed.append(stage)
    summaries = dict(acc.get("stage_summaries", {}) or {})
    summaries[stage] = summary
    delta.update({
        "completed_stages": completed,
        "stage_summaries": summaries,
        "progress_percentage": max(int(acc.get("progress_percentage", 0) or 0), progress),
    })
    if acc.get("pending_approval"):
        delta["current_stage"] = "awaiting_approval"
    return delta


def _max_iters(state: Dict[str, Any]) -> int:
    # LangGraph fills any TypedDict key the caller didn't supply with None,
    # so .get(key, default) alone isn't enough.
    value = state.get("max_repair_iterations")
    return int(value) if value is not None else MAX_REPAIR_ITERATIONS


def _attempts(state: Dict[str, Any]) -> int:
    return int(state.get("repair_attempts") or 0)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------------------
# Agent 1 - Discovery
# ---------------------------------------------------------------------------

async def discovery_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "")
    await _log(job_id, "discovery", "Discovery Agent: read-only AWS inventory, dependency graph and ownership triage.")
    delta, acc = await _run_steps("discovery", state, [
        ("intent_router", "classifying the request", intent_router_node),
        ("resource_explorer", "all-region inventory via Resource Explorer", resource_explorer_node),
        ("cloud_discovery", "read-only AWS discovery (Describe/Get/List)", cloud_discovery_node),
        ("graph_agent", "building the dependency graph", graph_agent_node),
        ("classification_agent", "classifying resource ownership", classification_agent_node),
    ])

    resources = acc.get("resources", []) or []
    cls = (acc.get("classification_results", {}) or {}).get("summary", {}) or {}
    edges = (acc.get("dependency_graph", {}) or {}).get("edge_count", 0) or 0
    summary = f"Found {_plural(len(resources), 'resource')}"
    if cls:
        summary += f": {cls.get('unmanaged', 0)} unmanaged, {cls.get('managed', 0)} managed, {cls.get('shared', 0)} shared"
    summary += f" · {_plural(edges, 'dependency')} · region {acc.get('region')}"
    inv = acc.get("resource_inventory", {}) or {}
    if inv.get("available"):
        summary += (
            f" · account-wide: {inv.get('total')}{'+' if inv.get('truncated') else ''} resources"
            f" in {len(inv.get('by_region') or {})} regions"
        )
    await _log(job_id, "discovery", f"Done. {summary}.")
    return _finish_stage("discovery", acc, delta, summary, 25)


# ---------------------------------------------------------------------------
# Agent 2 - Composer
# ---------------------------------------------------------------------------

async def composer_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "")
    await _log(job_id, "composer", "Composer Agent: planning adoption order and generating HCL with the local LLM.")
    delta, acc = await _run_steps("composer", state, [
        ("adoption_planning_agent", "planning adoption waves and import order", adoption_planning_agent_node),
        ("terraform_composer", "generating Terraform HCL", terraform_composer_node),
    ])

    files = acc.get("terraform_files", {}) or {}
    manifest = acc.get("generation_manifest", {}) or {}
    waves = len((acc.get("adoption_plan", {}) or {}).get("waves", []) or [])
    summary = (
        f"Generated {_plural(len(files), 'file')} · "
        f"{manifest.get('resources_generated', 0)} managed resources, "
        f"{manifest.get('resources_data_source', 0)} data sources, "
        f"{manifest.get('resources_review_required', 0)} for review"
    )
    if waves:
        summary += f" · {_plural(waves, 'wave')}"
    await _log(job_id, "composer", f"Done. {summary}.")
    return _finish_stage("composer", acc, delta, summary, 45)


# ---------------------------------------------------------------------------
# Agent 3 - Verifier
# ---------------------------------------------------------------------------

def _has_system_failure(state: Dict[str, Any]) -> bool:
    return any(c.get("check_name") == "system" for c in (state.get("validation_results", {}) or {}).get("checks", []))


def _needs_fix(state: Dict[str, Any]) -> bool:
    sec = state.get("security_results", {}) or {}
    val = state.get("validation_results", {}) or {}
    return (
        not val.get("passed", True)
        or sec.get("critical_count", 0) > 0
        or sec.get("high_count", 0) > 0
    )


def _record_iteration(acc: Dict[str, Any]) -> Dict[str, Any]:
    val = acc.get("validation_results", {}) or {}
    sec = acc.get("security_results", {}) or {}
    plan = acc.get("plan_equivalence_results", {}) or {}
    drift = acc.get("drift_results", {}) or {}

    plan_ran = bool(plan) and not plan.get("skipped")
    plan_changes = (
        sum(int(plan.get(k, 0) or 0) for k in ("create", "update", "replace", "destroy")) if plan_ran else None
    )
    high = int(sec.get("critical_count", 0) or 0) + int(sec.get("high_count", 0) or 0)
    return {
        "iteration": len(acc.get("verification_iterations", []) or []) + 1,
        "validation_passed": bool(val.get("passed", False)),
        "system_failure": _has_system_failure(acc),
        "high_findings": high,
        "total_findings": len(sec.get("findings", []) or []),
        "plan_changes": plan_changes,
        "drift_findings": len(drift.get("findings", []) or []),
        "halted_for_approval": bool(acc.get("pending_approval")),
        "passed": not _needs_fix(acc) and not acc.get("pending_approval"),
        "at": datetime.utcnow().isoformat(),
    }


def _iteration_summary(it: Dict[str, Any], max_iters: int) -> str:
    parts = [f"Pass {it['iteration']}/{max_iters + 1}"]
    if it["system_failure"]:
        parts.append("validation could not run (environment)")
    else:
        parts.append("validate ✓" if it["validation_passed"] else "validate ✗")
    parts.append(f"{it['high_findings']} high/critical" if it["high_findings"] else "no high findings")
    if it["plan_changes"] is not None:
        parts.append(f"plan: {_plural(it['plan_changes'], 'change')}")
    if it["halted_for_approval"]:
        parts.append("needs human approval")
    return " · ".join(parts)


async def verifier_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "")
    max_iters = _max_iters(state)
    pass_no = len(state.get("verification_iterations", []) or []) + 1
    await _log(
        job_id, "verifier",
        f"Verifier Agent pass {pass_no}: validate, drift check, plan equivalence, security policies.",
    )
    delta, acc = await _run_steps(
        "verifier",
        state,
        [
            ("validation_agent", "terraform fmt / init / validate", validation_agent_node),
            ("drift_reconciliation_agent", "comparing generated HCL to live AWS", drift_reconciliation_agent_node),
            ("plan_equivalence_agent", "terraform plan equivalence", plan_equivalence_agent_node),
            ("policy_agent", "tfsec / Checkov / Trivy / OPA", policy_agent_node),
        ],
        # Same halts as the old drift_gate/plan_gate edges: a destructive or
        # behavior-changing finding stops the pass before anything else runs.
        stop_if=lambda s: bool(s.get("pending_approval")),
    )

    iteration = _record_iteration(acc)
    iterations = list(acc.get("verification_iterations", []) or []) + [iteration]
    delta["verification_iterations"] = iterations
    acc["verification_iterations"] = iterations

    summary = _iteration_summary(iteration, max_iters)
    verdict = (
        "passed." if iteration["passed"]
        else "halting for human approval." if iteration["halted_for_approval"]
        else "issues found."
    )
    await _log(job_id, "verifier", f"{summary}: {verdict}")
    return _finish_stage("verifier", acc, delta, summary, 75)


def route_after_verify(state: Dict[str, Any]) -> str:
    if state.get("pending_approval"):
        return "halt"
    # An environment failure no HCL patch can fix - never spend a repair
    # cycle (or an LLM call) on it.
    if _has_system_failure(state):
        return "package"
    max_iters = _max_iters(state)
    if _needs_fix(state) and _attempts(state) < max_iters:
        return "repair"
    return "package"


# ---------------------------------------------------------------------------
# Agent 4 - Repair
# ---------------------------------------------------------------------------

async def repair_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "")
    before = dict(state.get("terraform_files", {}) or {})
    sec = state.get("security_results", {}) or {}
    targets = sorted({
        f.get("resource") for f in (sec.get("findings", []) or [])
        if f.get("resource") and str(f.get("severity", "")).upper() in ("CRITICAL", "HIGH")
    })
    cycle = _attempts(state) + 1
    max_iters = _max_iters(state)
    target_note = f" Targets: {', '.join(targets[:3])}{'…' if len(targets) > 3 else ''}." if targets else ""
    await _log(job_id, "repair", f"Repair Agent cycle {cycle}/{max_iters}.{target_note}")

    delta, acc = await _run_steps("repair", state, [
        ("repair_agent", "repairing HCL", repair_agent_node),
    ])

    after = acc.get("terraform_files", {}) or {}
    patched = sum(1 for k, v in after.items() if before.get(k) != v)
    summary = f"Cycle {cycle}/{max_iters}: patched {_plural(patched, 'file')}"
    if targets:
        summary += f" · {', '.join(targets[:2])}{'…' if len(targets) > 2 else ''}"
    if acc.get("pending_approval"):
        summary += " · escalated to human approval"
    await _log(job_id, "repair", f"Done. {summary}." + ("" if acc.get("pending_approval") else " Re-verifying."))
    return _finish_stage("repair", acc, delta, summary, 80)


def route_after_repair(state: Dict[str, Any]) -> str:
    # repair_agent_node escalates behavior_changing/destructive findings into
    # pending_approval instead of auto-fixing them - that must halt, not
    # re-verify (which would just rediscover the same findings) or package a
    # "COMPLETE" bundle nobody approved.
    if state.get("pending_approval"):
        return "halt"
    return "verifier"


# ---------------------------------------------------------------------------
# Output step (not an agent): cost estimate on the final HCL, docs, ZIP
# ---------------------------------------------------------------------------

async def package_outputs(state: Dict[str, Any]) -> Dict[str, Any]:
    """Also called directly by routers/scan.py::_resume_after_decision when a
    human approves a halted job - it's exactly the tail the graph never
    reached."""
    job_id = state.get("job_id", "")
    await _log(job_id, "package", "Packaging: cost estimate, documentation, import plan and ZIP bundle.")
    delta, acc = await _run_steps("package", state, [
        ("cost_agent", "Infracost cost estimate", cost_agent_node),
        ("documentation_agent", "writing docs and packaging the bundle", documentation_agent_node),
    ])
    files = len(acc.get("zip_manifest", []) or [])
    delta = _finish_stage("package", acc, delta, f"Bundle ready · {_plural(files, 'file')}", 100)
    delta["current_stage"] = "complete"
    return delta


def build_graph():
    """Compiles the four-agent LangGraph workflow."""
    workflow = StateGraph(TerraAgentState)

    workflow.add_node("discovery", discovery_agent)
    workflow.add_node("composer", composer_agent)
    workflow.add_node("verifier", verifier_agent)
    workflow.add_node("repair", repair_agent)
    workflow.add_node("package", package_outputs)

    workflow.set_entry_point("discovery")
    workflow.add_edge("discovery", "composer")
    workflow.add_edge("composer", "verifier")
    workflow.add_conditional_edges(
        "verifier",
        route_after_verify,
        {"repair": "repair", "package": "package", "halt": END},
    )
    workflow.add_conditional_edges(
        "repair",
        route_after_repair,
        {"verifier": "verifier", "halt": END},
    )
    workflow.add_edge("package", END)

    return workflow.compile()
