"""LangGraph StateGraph: four agents with a repair loop and a risk gate.

    infrastructure -> iac_engineering -> verification --PASS/INCOMPLETE/NEEDS_APPROVAL--> delivery -> END
                          ^                  |
                          +------ FAIL ------+   (validation errors, max N repair cycles)

1. Infrastructure Agent - read-only discovery (Resource Explorer + boto3),
   dependency graph, ownership classification.
2. IaC Engineering Agent - adoption plan + Terraform generation; on a FAIL
   verdict it repairs only the blocks that failed validation, and every fix
   must pass deterministic invariant checks (tools/hcl_invariants.py).
3. Verification & Risk Agent - judges, never edits. Validate first (stop there
   if it fails), then drift, plan equivalence and policy scans. Security
   findings are reported, never auto-fixed in adoption code. Fails closed: a
   crash, timeout, missing tool or unparseable output makes the verdict
   INCOMPLETE, never PASS.
4. Delivery & Approval Agent - risk gate (pending_approval pauses the job at
   AWAITING_APPROVAL until POST /scan/{id}/approve), then cost, docs and the
   bundle.

Repair lives in IaC Engineering, not Verification, so the verifier never
grades its own fix. Each agent runs *steps* - the original per-concern node
functions, which stay individually unit-tested.
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
from .resource_explorer_step import resource_explorer_node
from .terraform_composer import terraform_composer_node
from .validation_agent import validation_agent_node
from .validation_repair import repair_validation_node

# Previously hardcoded as `attempts < 2` in should_repair - same default.
MAX_REPAIR_ITERATIONS = int(os.getenv("TERRAAGENT_MAX_REPAIR_ITERATIONS", "2"))

# How often a long-running step (e.g. boto3 discovery stuck in retries) emits a
# "still working" log line, so the live log never goes silent for minutes.
HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("TERRAAGENT_HEARTBEAT_SECONDS", "15"))

# Display order.
AGENT_STAGES: Tuple[str, ...] = ("infrastructure", "iac_engineering", "verification", "delivery")
ALL_STAGES: Tuple[str, ...] = AGENT_STAGES


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
    current_stage: str  # one of AGENT_STAGES, "awaiting_approval", or "complete"
    completed_stages: List[str]
    stage_summaries: Dict[str, str]  # stage -> one-line human summary of its latest run
    verification_iterations: List[dict]  # one entry per verification pass (see _record_iteration)
    verification_verdict: str  # PASS | FAIL | INCOMPLETE | NEEDS_APPROVAL - latest pass
    repair_history: List[dict]  # one entry per repair cycle: fixed / rejected (invariants) / unresolved
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
        "current_stage": "infrastructure",
        "completed_stages": [],
        "stage_summaries": {},
        "verification_iterations": [],
        "verification_verdict": "",
        "repair_history": [],
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
# Agent 1 - Infrastructure: discover, graph, classify
# ---------------------------------------------------------------------------

async def infrastructure_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "")
    await _log(job_id, "infrastructure", "Infrastructure Agent: read-only discovery, dependency graph and ownership classification.")
    delta, acc = await _run_steps("infrastructure", state, [
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
    await _log(job_id, "infrastructure", f"Done. {summary}.")
    return _finish_stage("infrastructure", acc, delta, summary, 25)


# ---------------------------------------------------------------------------
# Agent 2 - IaC Engineering: adoption plan + generation, and repair
# ---------------------------------------------------------------------------

def _repair_requested(state: Dict[str, Any]) -> bool:
    iterations = state.get("verification_iterations") or []
    return bool(iterations) and iterations[-1].get("verdict") == VERDICT_FAIL


async def iac_engineering_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    """First visit: plan and generate. Later visits (sent back by the
    verifier): repair only the blocks that failed validation. Repair lives here,
    not in the verifier, so the verifier never grades its own fix."""
    job_id = state.get("job_id", "")
    max_iters = _max_iters(state)

    if _repair_requested(state):
        cycle = _attempts(state) + 1
        await _log(job_id, "iac_engineering", f"IaC Engineering Agent: repair cycle {cycle}/{max_iters} on validation errors.")
        delta, acc = await _run_steps("iac_engineering", state, [
            ("repair_agent", "repairing failing blocks", repair_validation_node),
        ])
        entry = (acc.get("repair_history") or [{}])[-1]
        fixed, rejected, unresolved = entry.get("fixed", []), entry.get("rejected", []), entry.get("unresolved", [])
        summary = f"Repair {cycle}/{max_iters}: fixed {_plural(len(fixed), 'block')}"
        if fixed:
            summary += f" ({', '.join(fixed[:2])}{'…' if len(fixed) > 2 else ''})"
        if rejected:
            summary += f" · {len(rejected)} fix{'es' if len(rejected) != 1 else ''} rejected by invariant checks"
        if unresolved:
            summary += f" · {len(unresolved)} unresolved"
        await _log(job_id, "iac_engineering", f"Done. {summary}. Sending back to verification.")
        return _finish_stage("iac_engineering", acc, delta, summary, 55)

    await _log(job_id, "iac_engineering", "IaC Engineering Agent: adoption plan, then Terraform generation.")
    delta, acc = await _run_steps("iac_engineering", state, [
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
    await _log(job_id, "iac_engineering", f"Done. {summary}.")
    return _finish_stage("iac_engineering", acc, delta, summary, 45)


# ---------------------------------------------------------------------------
# Agent 3 - Verification & Risk: judge only, never edit
# ---------------------------------------------------------------------------

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"  # validation errors - repairable by IaC Engineering
VERDICT_INCOMPLETE = "INCOMPLETE"  # fail closed: something couldn't be checked
VERDICT_NEEDS_APPROVAL = "NEEDS_APPROVAL"


def _has_system_failure(state: Dict[str, Any]) -> bool:
    return any(c.get("check_name") == "system" for c in (state.get("validation_results", {}) or {}).get("checks", []))


def _incomplete_reasons(acc: Dict[str, Any]) -> List[str]:
    """Everything that stops us from claiming the code was verified. Any
    crash, timeout, missing tool or unparseable output counts - never 'clean'."""
    reasons: List[str] = []
    if _has_system_failure(acc):
        system = next(c for c in acc["validation_results"]["checks"] if c.get("check_name") == "system")
        reasons.append(f"validation could not run ({str(system.get('output', ''))[:120]})")
    sec = acc.get("security_results") or {}
    for name in sec.get("scanners_skipped") or []:
        reasons.append(f"{name} not installed")
    for name, error in (sec.get("scanners_failed") or {}).items():
        reasons.append(f"{name} failed: {error}")
    plan = acc.get("plan_equivalence_results") or {}
    if plan and not plan.get("skipped"):
        init = next((c for c in plan.get("checks", []) or [] if c.get("check_name") == "init"), None)
        if init and not init.get("passed"):
            reasons.append("plan equivalence could not initialize")
    return reasons


def _record_iteration(acc: Dict[str, Any], validation_only: bool) -> Dict[str, Any]:
    val = acc.get("validation_results", {}) or {}
    sec = acc.get("security_results", {}) or {}
    plan = acc.get("plan_equivalence_results", {}) or {}
    drift = acc.get("drift_results", {}) or {}

    plan_ran = bool(plan) and not plan.get("skipped") and not validation_only
    plan_changes = (
        sum(int(plan.get(k, 0) or 0) for k in ("create", "update", "replace", "destroy")) if plan_ran else None
    )
    incomplete = _incomplete_reasons(acc)
    if acc.get("pending_approval"):
        verdict = VERDICT_NEEDS_APPROVAL
    elif _has_system_failure(acc):
        verdict = VERDICT_INCOMPLETE
    elif not val.get("passed", False):
        verdict = VERDICT_FAIL
    elif incomplete:
        verdict = VERDICT_INCOMPLETE
    else:
        verdict = VERDICT_PASS

    high = int(sec.get("critical_count", 0) or 0) + int(sec.get("high_count", 0) or 0)
    return {
        "iteration": len(acc.get("verification_iterations", []) or []) + 1,
        "verdict": verdict,
        "validation_passed": bool(val.get("passed", False)),
        "system_failure": _has_system_failure(acc),
        "checks_run": ["validate"] if validation_only else ["validate", "drift", "plan", "policy"],
        # Findings are reported (Security Posture), never repaired in adoption code.
        "high_findings": 0 if validation_only else high,
        "total_findings": 0 if validation_only else len(sec.get("findings", []) or []),
        "plan_changes": plan_changes,
        "drift_findings": 0 if validation_only else len(drift.get("findings", []) or []),
        "incomplete_reasons": incomplete,
        "halted_for_approval": verdict == VERDICT_NEEDS_APPROVAL,
        "passed": verdict == VERDICT_PASS,
        "at": datetime.utcnow().isoformat(),
    }


def _iteration_summary(it: Dict[str, Any], max_iters: int) -> str:
    parts = [f"Pass {it['iteration']}/{max_iters + 1}"]
    if it["system_failure"]:
        parts.append("validation could not run")
    else:
        parts.append("validate ✓" if it["validation_passed"] else "validate ✗")
    if "policy" in it["checks_run"]:
        parts.append(f"{it['high_findings']} high/critical reported" if it["high_findings"] else "no high findings")
        if it["plan_changes"] is not None:
            parts.append(f"plan: {_plural(it['plan_changes'], 'change')}")
    else:
        parts.append("later checks skipped until it validates")
    parts.append({
        VERDICT_PASS: "verified",
        VERDICT_FAIL: "sending back for repair",
        VERDICT_INCOMPLETE: "INCOMPLETE",
        VERDICT_NEEDS_APPROVAL: "needs human approval",
    }[it["verdict"]])
    return " · ".join(parts)


async def verification_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "")
    max_iters = _max_iters(state)
    pass_no = len(state.get("verification_iterations", []) or []) + 1
    await _log(job_id, "verification", f"Verification & Risk Agent pass {pass_no}: validate first, then drift, plan and policy.")

    # Validate first; if it fails, nothing else is worth running - hand the
    # errors straight back to IaC Engineering.
    delta, acc = await _run_steps("verification", state, [
        ("validation_agent", "terraform fmt / init / validate", validation_agent_node),
    ])
    validation_only = not (acc.get("validation_results") or {}).get("passed", False)
    if not validation_only:
        more, acc = await _run_steps(
            "verification",
            acc,
            [
                ("drift_reconciliation_agent", "comparing generated HCL to live AWS", drift_reconciliation_agent_node),
                ("plan_equivalence_agent", "terraform plan equivalence", plan_equivalence_agent_node),
                ("policy_agent", "Checkov / Trivy / OPA (report only)", policy_agent_node),
            ],
            # Same halts as before: a destructive or behavior-changing finding
            # stops the pass before anything else runs.
            stop_if=lambda s: bool(s.get("pending_approval")),
        )
        delta.update(more)

    iteration = _record_iteration(acc, validation_only)
    iterations = list(acc.get("verification_iterations", []) or []) + [iteration]
    delta["verification_iterations"] = iterations
    delta["verification_verdict"] = iteration["verdict"]
    acc["verification_iterations"] = iterations

    summary = _iteration_summary(iteration, max_iters)
    if iteration["incomplete_reasons"]:
        await _log(job_id, "verification", "Not verified: " + "; ".join(iteration["incomplete_reasons"]) + ".")
    await _log(job_id, "verification", summary + ".")
    return _finish_stage("verification", acc, delta, summary, 75)


def route_after_verification(state: Dict[str, Any]) -> str:
    iterations = state.get("verification_iterations") or []
    verdict = iterations[-1].get("verdict") if iterations else VERDICT_INCOMPLETE
    if verdict == VERDICT_FAIL and _attempts(state) < _max_iters(state):
        return "iac_engineering"
    # PASS, INCOMPLETE, NEEDS_APPROVAL, or out of repair budget: Delivery &
    # Approval decides whether to pause for a human or package.
    return "delivery"


# ---------------------------------------------------------------------------
# Agent 4 - Delivery & Approval: risk gate, then package
# ---------------------------------------------------------------------------

async def delivery_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    """Risk gate first: behavior-changing or destructive findings pause the job
    at AWAITING_APPROVAL until a human decides. Also called directly by
    routers/scan.py::_resume_after_decision once a human approves - with
    approval_decision set, the gate lets it through to packaging."""
    job_id = state.get("job_id", "")
    decision = (state.get("approval_decision") or {}).get("decision")

    if state.get("pending_approval") and decision != "approved":
        findings = len((state.get("pending_approval") or {}).get("findings", []) or [])
        await _log(job_id, "delivery", f"Risk gate: {_plural(findings, 'finding')} need a human decision. Pausing.")
        summaries = dict(state.get("stage_summaries") or {})
        summaries["delivery"] = f"Paused at the risk gate: {_plural(findings, 'finding')} awaiting approval"
        return {
            "status": "AWAITING_APPROVAL",
            "current_stage": "awaiting_approval",
            "current_agent": "awaiting_approval",
            "stage_summaries": summaries,
        }

    await _log(job_id, "delivery", "Delivery & Approval Agent: cost estimate, documentation, import plan and bundle.")
    delta, acc = await _run_steps("delivery", state, [
        ("cost_agent", "Infracost cost estimate", cost_agent_node),
        ("documentation_agent", "writing docs and packaging the bundle", documentation_agent_node),
    ])
    files = len(acc.get("zip_manifest", []) or [])
    verdict = acc.get("verification_verdict") or VERDICT_INCOMPLETE
    note = {
        VERDICT_PASS: "verified",
        VERDICT_FAIL: "NOT verified: validation still failing",
        VERDICT_INCOMPLETE: "NOT fully verified: see verification",
        VERDICT_NEEDS_APPROVAL: "approved by a human",
    }.get(verdict, verdict)
    delta = _finish_stage("delivery", acc, delta, f"Bundle ready · {_plural(files, 'file')} · {note}", 100)
    delta["current_stage"] = "complete"
    return delta



def build_graph():
    """Compiles the four-agent LangGraph workflow."""
    workflow = StateGraph(TerraAgentState)

    workflow.add_node("infrastructure", infrastructure_agent)
    workflow.add_node("iac_engineering", iac_engineering_agent)
    workflow.add_node("verification", verification_agent)
    workflow.add_node("delivery", delivery_agent)

    workflow.set_entry_point("infrastructure")
    workflow.add_edge("infrastructure", "iac_engineering")
    workflow.add_edge("iac_engineering", "verification")
    workflow.add_conditional_edges(
        "verification",
        route_after_verification,
        {"iac_engineering": "iac_engineering", "delivery": "delivery"},
    )
    workflow.add_edge("delivery", END)

    return workflow.compile()
