"""Router for scan initiation, status polling, and real-time SSE log streaming."""

import asyncio
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sse_starlette.sse import EventSourceResponse

import logging
from models.job import JobDecisionResponse, JobProgress, JobResults, PullRequestResponse
from models.scan import (
    ApprovalActionRequest,
    CreatePullRequestRequest,
    IntentAnalysisRequest,
    IntentAnalysisResponse,
    JobStatus,
    MergePullRequestRequest,
    MergePullRequestResponse,
    PullRequestDetailsResponse,
    PullRequestFileResponse,
    PullRequestReviewResponse,
    ScanRequest,
    ScanResponse,
)
from services.auth import require_api_key
from services.celery_app import run_scan_task
from services.database import (
    create_job_record, mark_job_complete, mark_job_failed, set_github_pr, set_hardening_pr, set_wave_pr,
)
from services.rate_limiter import limiter
from services.redis_client import redis_service
from tools.intent_analyzer import analyze_user_intent, parse_deterministic_intent

logger = logging.getLogger("terraagent.routers.scan")

router = APIRouter(prefix="/scan", tags=["scan"], dependencies=[Depends(require_api_key)])

# Mirrors the real node order in backend/agents/graph.py::build_graph() - kept
# here as the single source of truth for the DB-fallback path below, which
# has no other way to know which agents ran (repair_agent is conditional and
# was previously missing from this list even before classification_agent
# existed; keep both in sync with the graph rather than repeating this list).
# Must match agents/graph.py::ALL_STAGES (not imported, to keep LangGraph out
# of API startup).
ALL_STAGES = ("infrastructure", "iac_engineering", "verification", "delivery")

_FULL_AGENT_PIPELINE = (
    "intent_router", "cloud_discovery", "graph_agent", "classification_agent",
    "adoption_planning_agent", "terraform_composer", "validation_agent", "drift_reconciliation_agent",
    "plan_equivalence_agent", "policy_agent", "repair_agent", "cost_agent", "documentation_agent"
)


@router.post("/analyze-intent", response_model=IntentAnalysisResponse)
@limiter.limit("60/minute")
async def analyze_intent_endpoint(request: Request, intent_req: IntentAnalysisRequest):
    """Analyzes a DevOps natural-language request and extracts target resources, operation, and requested changes."""
    try:
        result = await analyze_user_intent(
            user_request=intent_req.user_request,
            region=intent_req.region,
            environment=intent_req.environment,
            resource_filters=intent_req.resource_filters,
        )
        return IntentAnalysisResponse(**result)
    except Exception as e:
        logger.warning(f"Intent analysis error ({e}), using fallback parser")
        fallback = parse_deterministic_intent(
            user_request=intent_req.user_request,
            region=intent_req.region,
            environment=intent_req.environment,
            resource_filters=intent_req.resource_filters,
        )
        return IntentAnalysisResponse(**fallback)


@router.post("", response_model=ScanResponse, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("10/hour")
async def start_scan(request: Request, scan_request: ScanRequest):
    """Dispatches a new background scan job to the LangGraph pipeline via Celery.

    Rate-limited to 10 scans/hour per client IP - a scan drives real AWS API
    calls plus several minutes of validation/security-scan/LLM work, so it's
    not something an accidental retry loop (or a malicious client) should be
    able to trigger without bound."""
    job_id = f"job-{uuid.uuid4().hex[:12]}"
    created_at = datetime.utcnow().isoformat()

    # Request payload prepared for worker (SecretStr extracted safely in memory)
    request_dict = {
        "aws_access_key": scan_request.aws_access_key.get_secret_value(),
        "aws_secret_key": scan_request.aws_secret_key.get_secret_value(),
        "aws_session_token": scan_request.aws_session_token.get_secret_value() if scan_request.aws_session_token else None,
        "region": scan_request.region,
        "environment": scan_request.environment or "production",
        "user_request": scan_request.user_request,
        "analyzed_intent": scan_request.analyzed_intent,
        "operation": scan_request.operation.value,
        "resource_filters": scan_request.resource_filters,
        "role_arn": scan_request.role_arn,
        "external_id": scan_request.external_id,
        "webhook_url": scan_request.webhook_url,
        "zip_password": scan_request.zip_password.get_secret_value() if scan_request.zip_password else None,
        "terraform_binary": scan_request.terraform_binary,
        "run_plan_equivalence": scan_request.run_plan_equivalence,
        "use_resource_explorer": scan_request.use_resource_explorer,
        "created_at": created_at
    }

    # Initialize job state
    initial_progress = {
        "job_id": job_id,
        "status": JobStatus.RUNNING.value,
        "progress_percentage": 5,
        "current_agent": "intent_router",
        "completed_agents": [],
        "created_at": created_at,
        "environment": scan_request.environment or "production",
        "user_request": scan_request.user_request,
        "analyzed_intent": scan_request.analyzed_intent,
    }
    await redis_service.set_job_state(job_id, initial_progress)
    create_job_record(
        job_id, scan_request.operation.value, scan_request.region, created_at,
        user_request=scan_request.user_request,
        environment=scan_request.environment or "production",
        analyzed_intent=scan_request.analyzed_intent,
    )

    # Dispatch Celery async task or run inline
    dispatched = False
    if redis_service._redis_available:
        try:
            run_scan_task.apply_async(args=[job_id, request_dict], retry=False)
            dispatched = True
        except Exception:
            dispatched = False

    if not dispatched:
        asyncio.create_task(run_scan_inline(job_id, request_dict))

    return ScanResponse(
        job_id=job_id,
        status=JobStatus.RUNNING,
        created_at=created_at,
        operation=scan_request.operation,
        region=scan_request.region,
        message="Scan initiated successfully"
    )


async def run_scan_inline(job_id: str, request_dict: dict):
    from services.pipeline import run_pipeline
    await run_pipeline(job_id, request_dict)


@router.get("/{job_id}/status", response_model=JobProgress)
async def get_scan_status(job_id: str):
    """Returns current status and agent step progress."""
    state = await redis_service.get_job_state(job_id)
    if not state:
        from services.database import get_job_record
        rec = get_job_record(job_id)
        if rec:
            # This DB-fallback path predates AWAITING_APPROVAL/REJECTED and
            # originally only recognized COMPLETE/FAILED - found live: once
            # Redis's 24h TTL on job state expires, a REJECTED job (which
            # genuinely ran the full pipeline tail - see
            # _resume_after_decision, which always calls documentation_agent
            # before overriding the final status) fell through to the
            # "still running" branch below and was reported as 50% RUNNING
            # forever, even though it's actually finished.
            #
            # REJECTED gets the same treatment as COMPLETE (it ran the same
            # nodes; only the final status differs). AWAITING_APPROVAL is
            # genuinely paused, not terminal, and - like FAILED - we don't
            # know from the DB record alone exactly which agents ran before
            # it halted, so completed_agents/current_agent stay unset rather
            # than falsely claiming either.
            ran_full_pipeline = rec.status in ("COMPLETE", "REJECTED")
            is_terminal = rec.status in ("COMPLETE", "FAILED", "REJECTED")
            return JobProgress(
                job_id=job_id,
                status=rec.status or JobStatus.COMPLETE,
                progress_percentage=100 if is_terminal else 50,
                current_agent="documentation_agent" if ran_full_pipeline else None,
                completed_agents=list(_FULL_AGENT_PIPELINE) if ran_full_pipeline else [],
                current_stage="complete" if ran_full_pipeline else None,
                completed_stages=list(ALL_STAGES) if ran_full_pipeline else [],
                created_at=rec.created_at or datetime.utcnow().isoformat(),
                error=rec.error
            )
        raise HTTPException(status_code=404, detail="Job not found")

    return JobProgress(
        job_id=job_id,
        status=state.get("status", JobStatus.PENDING),
        progress_percentage=state.get("progress_percentage", 50 if state.get("status") == "RUNNING" else 100),
        current_agent=state.get("current_agent"),
        completed_agents=state.get("completed_agents", []),
        current_stage=state.get("current_stage"),
        completed_stages=state.get("completed_stages", []) or [],
        stage_summaries=state.get("stage_summaries", {}) or {},
        verification_iterations=state.get("verification_iterations", []) or [],
        repair_attempts=state.get("repair_attempts", 0) or 0,
        max_repair_iterations=state.get("max_repair_iterations"),
        verification_verdict=state.get("verification_verdict") or None,
        repair_history=state.get("repair_history", []) or [],
        migration_safety=state.get("migration_safety"),
        security_posture=state.get("security_posture"),
        created_at=state.get("created_at", datetime.utcnow().isoformat()),
        error=state.get("error")
    )


@router.get("/{job_id}/results", response_model=JobResults)
async def get_scan_results(job_id: str):
    """Returns final inventory, dependency graph, and validation findings."""
    state = await redis_service.get_job_state(job_id)
    if not state:
        import json

        from services.database import get_job_record
        rec = get_job_record(job_id)
        if rec:
            return JobResults(
                job_id=job_id,
                status=rec.status or JobStatus.COMPLETE,
                operation=rec.operation or "generate",
                region=rec.region or "us-east-1",
                resources_count=rec.resources_discovered or 0,
                resources=[],
                classification_results=(
                    {"summary": json.loads(rec.classification_summary)} if rec.classification_summary else {}
                ),
                dependency_graph={},
                adoption_plan={"risk_score": rec.adoption_risk_score} if rec.adoption_risk_score is not None else {},
                validation_results={"passed": rec.validation_passed},
                plan_equivalence_results=(
                    {"confidence_score": rec.plan_equivalence_confidence}
                    if rec.plan_equivalence_confidence is not None else {}
                ),
                security_results={"findings": []},
                pending_approval=(
                    json.loads(rec.pending_approval_summary) if rec.pending_approval_summary else None
                ),
                approval_decision=(
                    json.loads(rec.approval_decision_summary) if rec.approval_decision_summary else None
                ),
                github_pr=(
                    {"pr_url": rec.github_pr_url, "pr_number": rec.github_pr_number}
                    if rec.github_pr_url else None
                ),
                github_wave_prs=(
                    json.loads(rec.github_wave_prs_summary) if rec.github_wave_prs_summary else {}
                ),
                zip_available=bool(rec.zip_generated),
                download_url=f"/api/download/{job_id}" if rec.zip_generated else None,
                zip_sha256=None,
                zip_manifest=[]
            )
        raise HTTPException(status_code=404, detail="Job not found")


    resources = state.get("resources", [])
    return JobResults(
        job_id=job_id,
        status=state.get("status", JobStatus.COMPLETE),
        operation=state.get("operation", "generate"),
        region=state.get("region", "us-east-1"),
        resources_count=len(resources),
        resources=resources,
        classification_results=state.get("classification_results", {}),
        dependency_graph=state.get("dependency_graph", {}),
        adoption_plan=state.get("adoption_plan", {}),
        resource_inventory=state.get("resource_inventory") or {},
        infra_model=state.get("infra_model") or {},
        config_crosscheck=state.get("config_crosscheck") or {},
        requested_region=state.get("requested_region"),
        generation_manifest=state.get("generation_manifest"),
        validation_results=state.get("validation_results", {}),
        drift_results=state.get("drift_results", {}),
        plan_equivalence_results=state.get("plan_equivalence_results", {}),
        security_results=state.get("security_results", {}),
        migration_safety=state.get("migration_safety"),
        security_posture=state.get("security_posture"),
        pending_approval=state.get("pending_approval"),
        approval_request=state.get("approval_request"),
        approval_decision=state.get("approval_decision"),
        hardening=state.get("hardening") or {},
        user_request=state.get("user_request"),
        analyzed_intent=state.get("analyzed_intent") or state.get("intent") or None,
        cost_results=state.get("cost_results") or {},
        github_pr=state.get("github_pr"),
        github_wave_prs=state.get("github_wave_prs", {}),
        github_hardening_pr=state.get("github_hardening_pr"),
        zip_available=bool(state.get("zip_path")),
        download_url=f"/api/download/{job_id}" if state.get("zip_path") else None,
        zip_sha256=state.get("zip_sha256"),
        zip_manifest=state.get("zip_manifest", [])
    )


async def _awaiting_state(job_id: str) -> Dict[str, Any]:
    state = await redis_service.get_job_state(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="Job not found")
    if state.get("status") != JobStatus.AWAITING_APPROVAL.value:
        raise HTTPException(
            status_code=409,
            detail=f"Job is not awaiting approval (current status: {state.get('status')})"
        )
    return state


def _validate_resource_decisions(state: Dict[str, Any], decisions: Dict[str, str]) -> None:
    """Approving means deciding every resource the gate listed as in Review,
    each with a choice allowed for it (an unsupported type can only be
    excluded) - nothing else."""
    review = (state.get("approval_request") or {}).get("review_resources") or []
    allowed = {r["resource_id"]: r.get("choices") or ["manage", "reference", "exclude"] for r in review}
    missing = sorted(set(allowed) - set(decisions))
    unknown = sorted(set(decisions) - set(allowed))
    invalid = sorted(rid for rid, d in decisions.items() if rid in allowed and d not in allowed[rid])
    if missing or unknown or invalid:
        raise HTTPException(status_code=422, detail={
            "message": "Every resource in Review needs one allowed decision",
            "missing": missing, "unknown": unknown, "not_allowed": invalid,
        })


async def _dispatch_resume(job_id: str, decision: Dict[str, Any], aws_credentials: Optional[Dict[str, Any]]) -> None:
    from services.celery_app import resume_scan_task
    if redis_service._redis_available:
        try:
            resume_scan_task.apply_async(args=[job_id, decision, aws_credentials], retry=False)
            return
        except Exception:
            pass
    asyncio.create_task(_resume_inline(job_id, decision, aws_credentials))


async def _resume_inline(job_id: str, decision: Dict[str, Any], aws_credentials: Optional[Dict[str, Any]]) -> None:
    from services.pipeline import resume_pipeline
    await resume_pipeline(job_id, decision, aws_credentials)


async def _decide(job_id: str, state: Dict[str, Any], decision: Dict[str, Any],
                  aws_credentials: Optional[Dict[str, Any]], status_now: JobStatus) -> None:
    from services.checkpoints import load_checkpoint
    if not load_checkpoint(job_id):
        raise HTTPException(
            status_code=409,
            detail="This paused run has no saved checkpoint (it was paused by an older version) - re-run the scan",
        )
    # Flip the status first so a second click can't resume the same run twice.
    await redis_service.set_job_state(job_id, {**state, "status": status_now.value, "approval_decision": decision})
    await redis_service.publish_log(
        job_id,
        f"[AGENT:system] Human {decision['decision']} the gate - resuming the paused run.",
        agent_name="system",
    )
    await _dispatch_resume(job_id, decision, aws_credentials)


@router.post("/{job_id}/approve", response_model=JobDecisionResponse)
async def approve_scan(job_id: str, body: ApprovalActionRequest = ApprovalActionRequest()):
    """Resume a run paused at the Delivery & Approval Agent's risk gate. The
    body must decide every resource in Review. Review decisions that add code
    send the run back through IaC Engineering and Verification; otherwise it
    packages the bundle. Never runs terraform apply or import."""
    state = await _awaiting_state(job_id)
    _validate_resource_decisions(state, body.resource_decisions)
    creds = None
    if body.aws_access_key and body.aws_secret_key:
        creds = {
            "access_key": body.aws_access_key.get_secret_value(),
            "secret_key": body.aws_secret_key.get_secret_value(),
            "session_token": body.aws_session_token.get_secret_value() if body.aws_session_token else None,
        }
    decision = {"decision": "approved", "reason": body.reason, "resource_decisions": body.resource_decisions,
                "decided_at": datetime.utcnow().isoformat()}
    await _decide(job_id, state, decision, creds, JobStatus.RUNNING)
    return JobDecisionResponse(job_id=job_id, status=JobStatus.RUNNING, message="Approved - resuming pipeline.")


@router.post("/{job_id}/reject", response_model=JobDecisionResponse)
async def reject_scan(job_id: str, body: ApprovalActionRequest = ApprovalActionRequest()):
    """Human rejection - the run ends here. It still writes the audit README,
    but never an adoptable bundle."""
    state = await _awaiting_state(job_id)
    decision = {"decision": "rejected", "reason": body.reason, "resource_decisions": {},
                "decided_at": datetime.utcnow().isoformat()}
    await _decide(job_id, state, decision, None, JobStatus.REJECTED)
    return JobDecisionResponse(job_id=job_id, status=JobStatus.REJECTED, message="Rejected - pipeline will not continue.")


@router.post("/{job_id}/pull-request", response_model=PullRequestResponse)
@limiter.limit("20/hour")
async def create_pull_request(request: Request, job_id: str, body: CreatePullRequestRequest):
    """Opens a reviewable GitHub Pull Request from a completed job's
    generated Terraform/OpenTofu files - the engineering deliverable this
    feature exists for, offered alongside (not instead of) the ZIP download.

    The GitHub token is supplied here, at PR-creation time, by whoever is
    ready to open it - never earlier. It is extracted from `body.github_token`
    (a SecretStr) only in this function, passed straight into
    github_client.create_adoption_pr, and discarded when this handler
    returns: it is never added to TerraAgentState, never round-tripped
    through redis_service (which would scrub it to "[REDACTED]" anyway, same
    as aws_credentials/zip_password), and never logged. Only the resulting
    PR url/number/branch - never the token - get persisted, into both the
    Redis job state (so GET /results can show it) and JobRecord (for
    durability past Redis's TTL)."""
    state = await redis_service.get_job_state(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="Job not found")
    if state.get("status") != JobStatus.COMPLETE.value:
        raise HTTPException(
            status_code=409,
            detail=f"Job must be COMPLETE before a pull request can be opened (current status: {state.get('status')})"
        )
    tf_files = state.get("terraform_files") or {}
    if not tf_files:
        raise HTTPException(status_code=409, detail="No generated Terraform files available for this job")
    if body.kind == "hardening":
        return await _create_hardening_pr(job_id, state, body)

    wave_info = None
    if body.wave is not None:
        waves = (state.get("adoption_plan") or {}).get("waves") or []
        wave_info = next((w for w in waves if w.get("wave") == body.wave), None)
        if wave_info is None:
            raise HTTPException(
                status_code=404,
                detail=f"Wave {body.wave} does not exist in this job's adoption plan"
            )

    from services.github_client import GitHubPullRequestError, create_adoption_pr
    from tools.credential_scrubber import CredentialScrubber

    try:
        pr_info = await create_adoption_pr(
            github_token=body.github_token.get_secret_value(),
            repo=body.repo,
            job_id=job_id,
            tf_files=tf_files,
            docs=state.get("documentation") or {},
            adoption_plan=state.get("adoption_plan") or {},
            plan_equivalence_results=state.get("plan_equivalence_results") or {},
            drift_results=state.get("drift_results") or {},
            security_results=state.get("security_results") or {},
            cost_results=state.get("cost_results") or {},
            pending_approval=state.get("pending_approval"),
            approval_decision=state.get("approval_decision"),
            resources=state.get("resources") or [],
            wave=wave_info,
            base_branch=body.base_branch,
            migration_safety=state.get("migration_safety"),
            security_posture=state.get("security_posture"),
            infra_model=state.get("infra_model"),
        )
    except GitHubPullRequestError as e:
        # GitHub's own error text can legitimately echo back request details
        # (an invalid path, a malformed ref) but never the token (it only
        # ever appears in the outgoing Authorization header) - scrubbed
        # anyway as the same discipline applied to every other externally-
        # sourced error message in this codebase.
        raise HTTPException(status_code=502, detail=CredentialScrubber.scrub_text(str(e)))

    if wave_info is not None and body.wave is not None:
        wave_prs = dict(state.get("github_wave_prs") or {})
        wave_prs[str(body.wave)] = pr_info
        state["github_wave_prs"] = wave_prs
        set_wave_pr(job_id, body.wave, pr_info)
    else:
        state["github_pr"] = pr_info
        set_github_pr(job_id, pr_info)
    await redis_service.set_job_state(job_id, state)
    await redis_service.publish_log(
        job_id,
        f"[AGENT:system] GitHub pull request opened"
        f"{f' for wave {body.wave}' if wave_info is not None else ''}: {pr_info['pr_url']}",
        agent_name="system"
    )

    return PullRequestResponse(job_id=job_id, **pr_info)


async def _create_hardening_pr(job_id: str, state: Dict[str, Any], body: CreatePullRequestRequest) -> PullRequestResponse:
    """The Hardening PR stacks on the adoption PR's branch - so the adoption
    PR has to exist first, and there has to be validated hardening code."""
    hardening = state.get("hardening") or {}
    if not hardening.get("files"):
        raise HTTPException(status_code=409, detail="This job has no validated hardening changes to open a PR for")
    adoption_pr = state.get("github_pr") or {}
    if not adoption_pr.get("branch"):
        raise HTTPException(status_code=409, detail="Open the adoption PR first - hardening is applied on top of it")

    from services.github_client import GitHubPullRequestError, create_hardening_pr
    from tools.credential_scrubber import CredentialScrubber
    try:
        pr_info = await create_hardening_pr(
            github_token=body.github_token.get_secret_value(),
            repo=body.repo,
            job_id=job_id,
            hardening=hardening,
            adoption_pr=adoption_pr,
        )
    except GitHubPullRequestError as e:
        raise HTTPException(status_code=502, detail=CredentialScrubber.scrub_text(str(e)))

    state["github_hardening_pr"] = pr_info
    set_hardening_pr(job_id, pr_info)
    await redis_service.set_job_state(job_id, state)
    await redis_service.publish_log(
        job_id, f"[AGENT:system] GitHub hardening pull request opened: {pr_info['pr_url']}", agent_name="system")
    return PullRequestResponse(job_id=job_id, **pr_info)


_PR_STATE_KEYS = {"adoption": "github_pr", "hardening": "github_hardening_pr"}


def _own_pr(state: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """This job's own PR of the given kind. The PR-lifecycle endpoints only
    ever act on these - never on a repo/PR number a caller supplies."""
    pr = dict(state.get(_PR_STATE_KEYS[kind]) or {})
    if not pr.get("repo") or not pr.get("pr_number"):
        raise HTTPException(status_code=404, detail=f"This job has no {kind} pull request yet - open it first")
    return pr


def _github_token_header(request: Request) -> str:
    # Only this dedicated header - never logged, never persisted.
    return (request.headers.get("X-GitHub-Token") or "").strip()


def _approved_in_github(reviews: List[Dict[str, Any]]) -> bool:
    """At least one reviewer's latest review is APPROVED and nobody's latest
    is CHANGES_REQUESTED (GitHub reports every review in order)."""
    latest: Dict[str, str] = {}
    for r in reviews:
        if r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[str(r.get("user"))] = r["state"]
    states = set(latest.values())
    return "APPROVED" in states and "CHANGES_REQUESTED" not in states


def _details_response(job_id: str, pr: Dict[str, Any], details: Optional[Dict[str, Any]],
                      diff_text: Optional[str], workflow_runs: List[Dict[str, Any]]) -> PullRequestDetailsResponse:
    d = details or {}
    files = d.get("changed_files") or [{"filename": f} for f in pr.get("changed_files") or []]
    return PullRequestDetailsResponse(
        job_id=job_id,
        repo=pr["repo"],
        pr_number=pr["pr_number"],
        title=d.get("title") or pr.get("pr_title") or f"PR #{pr['pr_number']}",
        state=d.get("state") or pr.get("status") or "open",
        html_url=d.get("html_url") or pr.get("pr_url") or f"https://github.com/{pr['repo']}/pull/{pr['pr_number']}",
        body=d.get("body"),
        head_branch=d.get("head_branch") or pr.get("branch") or "",
        base_branch=d.get("base_branch") or pr.get("base_branch") or "main",
        head_sha=d.get("head_sha") or pr.get("commit_sha"),
        mergeable=d.get("mergeable"),
        mergeable_state=d.get("mergeable_state"),
        merged=bool(d.get("merged", pr.get("merged", False))),
        merged_at=d.get("merged_at") or pr.get("merged_at"),
        merge_commit_sha=d.get("merge_commit_sha") or pr.get("merge_commit_sha"),
        additions=d.get("additions", 0),
        deletions=d.get("deletions", 0),
        changed_files_count=d.get("changed_files_count", len(files)),
        changed_files=[PullRequestFileResponse(**f) for f in files],
        reviews=[PullRequestReviewResponse(**r) for r in d.get("reviews") or []],
        diff=diff_text,
        workflow_runs=workflow_runs,
    )


@router.get("/{job_id}/pull-request", response_model=PullRequestDetailsResponse)
async def get_pull_request_status(request: Request, job_id: str, kind: Literal["adoption", "hardening"] = "adoption"):
    """Live status of this job's own adoption or hardening PR: merge state,
    changed files, diff, reviews and recent workflow runs on the base branch.
    Live data needs a GitHub token in the X-GitHub-Token header (used for this
    request only); without it, the cached PR record is returned."""
    state = await redis_service.get_job_state(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="Job not found")
    pr = _own_pr(state, kind)
    token = _github_token_header(request)
    if not token:
        return _details_response(job_id, pr, None, None, [])

    from services.github_client import GitHubPullRequestError, get_pr_details, get_pr_diff, get_workflow_runs
    from tools.credential_scrubber import CredentialScrubber

    try:
        details = await get_pr_details(github_token=token, repo=pr["repo"], pr_number=pr["pr_number"])
    except GitHubPullRequestError as e:
        raise HTTPException(status_code=502, detail=CredentialScrubber.scrub_text(str(e)))
    diff_text: Optional[str] = None
    try:
        diff_text = await get_pr_diff(github_token=token, repo=pr["repo"], pr_number=pr["pr_number"])
    except Exception as e:
        logger.warning(f"[{job_id}] Could not fetch PR diff: {CredentialScrubber.scrub_text(str(e))}")
    workflow_runs: List[Dict[str, Any]] = []
    try:
        workflow_runs = await get_workflow_runs(
            github_token=token, repo=pr["repo"], branch=details.get("base_branch") or "main")
    except Exception as e:
        logger.warning(f"[{job_id}] Could not fetch workflow runs: {CredentialScrubber.scrub_text(str(e))}")

    pr["status"] = details.get("state", "open")
    if details.get("merged"):
        pr.update(merged=True, merge_commit_sha=details.get("merge_commit_sha"), merged_at=details.get("merged_at"))
    state[_PR_STATE_KEYS[kind]] = pr
    await redis_service.set_job_state(job_id, state)
    return _details_response(job_id, pr, details, diff_text, workflow_runs)


@router.post("/{job_id}/pull-request/merge", response_model=MergePullRequestResponse)
@limiter.limit("20/hour")
async def merge_pull_request(request: Request, job_id: str, body: MergePullRequestRequest):
    """Merge this job's own PR - a human action with guard rails, because the
    team's pipeline may apply on merge. TerraAgent itself still never runs
    terraform apply. Refused unless: confirm is true; for hardening, the
    adoption PR is merged and the hardening PR now targets the same base; and
    GitHub reports the PR open, mergeable, and approved by a reviewer (in
    GitHub - TerraAgent does not approve its own PRs)."""
    if not body.confirm:
        raise HTTPException(status_code=422, detail="Confirm the merge: your pipeline may apply this change once merged")
    state = await redis_service.get_job_state(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="Job not found")
    pr = _own_pr(state, body.kind)
    adoption = state.get("github_pr") or {}
    if body.kind == "hardening" and not adoption.get("merged"):
        raise HTTPException(status_code=409, detail="Merge the adoption PR first - hardening is applied on top of it")

    from services.github_client import GitHubPullRequestError, get_pr_details, get_workflow_runs, merge_pr
    from tools.credential_scrubber import CredentialScrubber

    token = body.github_token.get_secret_value()
    try:
        details = await get_pr_details(github_token=token, repo=pr["repo"], pr_number=pr["pr_number"])
    except GitHubPullRequestError as e:
        raise HTTPException(status_code=502, detail=CredentialScrubber.scrub_text(str(e)))
    if details.get("merged") or details.get("state") != "open":
        raise HTTPException(status_code=409, detail=f"The PR is {'already merged' if details.get('merged') else 'not open'}")
    if body.kind == "hardening" and details.get("base_branch") != adoption.get("base_branch"):
        raise HTTPException(
            status_code=409,
            detail=(f"The hardening PR still targets '{details.get('base_branch')}'. Retarget it onto "
                    f"'{adoption.get('base_branch')}' first (GitHub does this when the adoption branch is deleted)."),
        )
    if details.get("mergeable") is not True:
        raise HTTPException(
            status_code=409,
            detail=f"GitHub doesn't report the PR as mergeable yet ({details.get('mergeable_state') or 'unknown'})",
        )
    if not _approved_in_github(details.get("reviews") or []):
        raise HTTPException(status_code=409, detail="The PR needs an approving review in GitHub before it can be merged")

    try:
        merge_result = await merge_pr(
            github_token=token, repo=pr["repo"], pr_number=pr["pr_number"],
            merge_method=body.merge_method, commit_title=body.commit_title, commit_message=body.commit_message,
        )
    except GitHubPullRequestError as e:
        raise HTTPException(status_code=502, detail=CredentialScrubber.scrub_text(str(e)))

    base_branch = details.get("base_branch") or pr.get("base_branch") or "main"
    workflow_runs: List[Dict[str, Any]] = []
    try:
        workflow_runs = await get_workflow_runs(github_token=token, repo=pr["repo"], branch=base_branch)
    except Exception as e:
        logger.warning(f"[{job_id}] Failed to fetch workflow runs after merge: {CredentialScrubber.scrub_text(str(e))}")

    pr.update(status="merged", merged=True, merge_commit_sha=merge_result.get("sha"),
              merged_at=datetime.utcnow().isoformat())
    state[_PR_STATE_KEYS[body.kind]] = pr
    await redis_service.set_job_state(job_id, state)
    await redis_service.publish_log(
        job_id,
        f"[AGENT:system] {body.kind.title()} PR #{pr['pr_number']} merged into {base_branch} by a human "
        f"(commit {merge_result.get('sha')}). Any apply happens in the team's own pipeline.",
        agent_name="system",
    )
    return MergePullRequestResponse(
        job_id=job_id, merged=True, sha=merge_result.get("sha"),
        message=merge_result.get("message", "Pull Request successfully merged"), workflow_runs=workflow_runs,
    )


@router.get("/{job_id}/logs")
async def stream_logs(job_id: str):
    """SSE endpoint streaming live agent log lines."""
    async def event_generator():
        async for log_msg in redis_service.subscribe_logs(job_id):
            yield {"event": "message", "data": log_msg}

    return EventSourceResponse(event_generator())
