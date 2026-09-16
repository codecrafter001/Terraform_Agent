"""Router for scan initiation, status polling, and real-time SSE log streaming."""

import asyncio
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sse_starlette.sse import EventSourceResponse

from models.job import JobDecisionResponse, JobProgress, JobResults, PullRequestResponse
from models.scan import ApprovalActionRequest, CreatePullRequestRequest, JobStatus, ScanRequest, ScanResponse
from services.auth import require_api_key
from services.celery_app import run_scan_task
from services.database import create_job_record, mark_job_complete, mark_job_failed, set_github_pr, set_wave_pr
from services.rate_limiter import limiter
from services.redis_client import redis_service

router = APIRouter(prefix="/scan", tags=["scan"], dependencies=[Depends(require_api_key)])

# Mirrors the real node order in backend/agents/graph.py::build_graph() - kept
# here as the single source of truth for the DB-fallback path below, which
# has no other way to know which agents ran (repair_agent is conditional and
# was previously missing from this list even before classification_agent
# existed; keep both in sync with the graph rather than repeating this list).
_FULL_AGENT_PIPELINE = (
    "intent_router", "cloud_discovery", "graph_agent", "classification_agent",
    "adoption_planning_agent", "terraform_composer", "validation_agent", "drift_reconciliation_agent",
    "plan_equivalence_agent", "policy_agent", "repair_agent", "cost_agent", "documentation_agent"
)


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
        "operation": scan_request.operation.value,
        "resource_filters": scan_request.resource_filters,
        "role_arn": scan_request.role_arn,
        "webhook_url": scan_request.webhook_url,
        "zip_password": scan_request.zip_password.get_secret_value() if scan_request.zip_password else None,
        "terraform_binary": scan_request.terraform_binary,
        "run_plan_equivalence": scan_request.run_plan_equivalence,
        "created_at": created_at
    }

    # Initialize job state
    initial_progress = {
        "job_id": job_id,
        "status": JobStatus.RUNNING.value,
        "progress_percentage": 5,
        "current_agent": "intent_router",
        "completed_agents": [],
        "created_at": created_at
    }
    await redis_service.set_job_state(job_id, initial_progress)
    create_job_record(job_id, scan_request.operation.value, scan_request.region, created_at)

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
    from agents.graph import build_graph
    from routers.metrics import record_job_outcome
    try:
        app = build_graph()
        initial_state = {
            "job_id": job_id,
            "created_at": request_dict.get("created_at", datetime.utcnow().isoformat()),
            "operation": request_dict.get("operation", "generate"),
            "region": request_dict.get("region", "us-east-1"),
            "resource_filters": request_dict.get("resource_filters", ["EC2", "VPC", "S3", "RDS", "IAM", "SG"]),
            "aws_credentials": {
                "access_key": request_dict.get("aws_access_key"),
                "secret_key": request_dict.get("aws_secret_key"),
                "session_token": request_dict.get("aws_session_token")
            },
            "aws_endpoint_url": request_dict.get("aws_endpoint_url"),
            "role_arn": request_dict.get("role_arn"),
            "webhook_url": request_dict.get("webhook_url"),
            "zip_password": request_dict.get("zip_password"),
            "terraform_binary": request_dict.get("terraform_binary", "terraform"),
            "run_plan_equivalence": request_dict.get("run_plan_equivalence", False),
            "intent": {},
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
            "agent_timings": {}
        }
        final_state = await app.ainvoke(initial_state)
        await redis_service.set_job_state(job_id, final_state)
        mark_job_complete(job_id, final_state)
        record_job_outcome(final_state.get("status", "COMPLETE"), final_state.get("agent_timings"))

        webhook_url = final_state.get("webhook_url")
        if webhook_url:
            from services.webhook import send_scan_summary
            await send_scan_summary(webhook_url, job_id, final_state)
    except Exception as e:
        # This catches failures from cloud_discovery_node, the one node that
        # actually holds raw AWS credentials - same gap already fixed in
        # services/celery_app.py::run_scan_task's exception handler, just
        # never applied here too. Scrub before it ever leaves this function:
        # it gets published live over Redis pub/sub, persisted into Postgres,
        # and served back verbatim by routers/jobs.py and this file's own
        # DB-fallback path.
        from tools.credential_scrubber import CredentialScrubber
        error_msg = CredentialScrubber.scrub_text(str(e))
        await redis_service.publish_log(job_id, f"Pipeline error: {error_msg}", "error")
        await redis_service.set_job_state(job_id, {
            "job_id": job_id,
            "status": JobStatus.FAILED.value,
            "error": error_msg
        })
        mark_job_failed(job_id, error_msg)
        record_job_outcome("FAILED")


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
        generation_manifest=state.get("generation_manifest"),
        validation_results=state.get("validation_results", {}),
        drift_results=state.get("drift_results", {}),
        plan_equivalence_results=state.get("plan_equivalence_results", {}),
        security_results=state.get("security_results", {}),
        pending_approval=state.get("pending_approval"),
        approval_decision=state.get("approval_decision"),
        github_pr=state.get("github_pr"),
        github_wave_prs=state.get("github_wave_prs", {}),
        zip_available=bool(state.get("zip_path")),
        download_url=f"/api/download/{job_id}" if state.get("zip_path") else None,
        zip_sha256=state.get("zip_sha256"),
        zip_manifest=state.get("zip_manifest", [])
    )


@router.post("/{job_id}/approve", response_model=JobDecisionResponse)
async def approve_scan(job_id: str, body: ApprovalActionRequest = ApprovalActionRequest()):
    """Human approval for a job halted by plan_equivalence_agent or repair_agent
    over a destructive/behavior_changing finding. Resumes the remaining
    pipeline tail (cost estimation + documentation/ZIP packaging) - the two
    nodes the graph never reached, since it routed straight to END instead of
    continuing past the halt (see agents/graph.py::plan_gate/repair_or_done)."""
    state = await redis_service.get_job_state(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="Job not found")
    if state.get("status") != JobStatus.AWAITING_APPROVAL.value:
        raise HTTPException(
            status_code=409,
            detail=f"Job is not awaiting approval (current status: {state.get('status')})"
        )

    asyncio.create_task(_resume_after_decision(job_id, state, approved=True, reason=body.reason))

    return JobDecisionResponse(job_id=job_id, status=JobStatus.RUNNING, message="Approved - resuming pipeline.")


@router.post("/{job_id}/reject", response_model=JobDecisionResponse)
async def reject_scan(job_id: str, body: ApprovalActionRequest = ApprovalActionRequest()):
    """Human rejection - the job stops here permanently. Still runs
    documentation_agent once so there's an audit-trail README explaining
    what was found and why it was rejected, but never proceeds to a
    completed, adoptable bundle."""
    state = await redis_service.get_job_state(job_id)
    if not state:
        raise HTTPException(status_code=404, detail="Job not found")
    if state.get("status") != JobStatus.AWAITING_APPROVAL.value:
        raise HTTPException(
            status_code=409,
            detail=f"Job is not awaiting approval (current status: {state.get('status')})"
        )

    asyncio.create_task(_resume_after_decision(job_id, state, approved=False, reason=body.reason))

    return JobDecisionResponse(job_id=job_id, status=JobStatus.REJECTED, message="Rejected - pipeline will not continue.")


async def _resume_after_decision(job_id: str, state: Dict[str, Any], approved: bool, reason: Optional[str]):
    """Runs the tail the halted graph never reached. Called as a background
    task from approve_scan/reject_scan so the HTTP request returns
    immediately - cost_agent/documentation_agent can take real time (Infracost
    subprocess, an LLM call, ZIP packaging)."""
    from agents.cost_agent import cost_agent_node
    from agents.documentation_agent import documentation_agent_node
    from agents.graph import _timed
    from routers.metrics import record_job_outcome

    # _timed wraps each node the exact same way build_graph() does for every
    # other node in the pipeline - records its duration into agent_timings
    # and calls record_scan_error on failure. Calling cost_agent_node/
    # documentation_agent_node directly (as this function did before) meant
    # neither ever showed up in agent_timings for a resumed job, and a
    # failure here never incremented scan_errors_total.
    cost_node = _timed("cost_agent", cost_agent_node)
    documentation_node = _timed("documentation_agent", documentation_agent_node)

    state = dict(state)
    state["approval_decision"] = {
        "decision": "approved" if approved else "rejected",
        "reason": reason,
        "decided_at": datetime.utcnow().isoformat(),
    }

    # redis_service.set_job_state round-trips every persisted job state
    # through CredentialScrubber.scrub_dict, which redacts any field named
    # "zip_password" to the literal string "[REDACTED]" before it's ever
    # written to Redis. That's correct for keeping it out of Redis/logs, but
    # it means the value read back here (via get_job_state, the only way
    # this endpoint can see the halted job's state at all) is no longer the
    # user's real password - encrypting the ZIP with the literal string
    # "[REDACTED]" would be worse than not encrypting it at all (a fixed,
    # guessable password on every resumed job). Treat it as "no password"
    # instead, same as if the user had never supplied one.
    if state.get("zip_password") == "[REDACTED]":
        state["zip_password"] = None

    try:
        if approved:
            state["status"] = "RUNNING"
            await redis_service.set_job_state(job_id, state)
            await redis_service.publish_log(
                job_id, "[AGENT:system] Human approval received - resuming pipeline.", agent_name="system"
            )

            result = await cost_node(state)
            state = {**state, **result}

            result = await documentation_node(state)
            state = {**state, **result}  # sets status back to COMPLETE
        else:
            state["status"] = "REJECTED"
            await redis_service.set_job_state(job_id, state)
            note = f": {reason}" if reason else "."
            await redis_service.publish_log(
                job_id,
                f"[AGENT:system] Human rejected the pending findings{note} Pipeline halted permanently.",
                agent_name="system",
            )

            result = await documentation_node(state)
            state = {**state, **result}
            state["status"] = "REJECTED"  # documentation_agent_node hardcodes COMPLETE - override

        await redis_service.set_job_state(job_id, state)
        mark_job_complete(job_id, state)
        record_job_outcome(state.get("status", "COMPLETE"), state.get("agent_timings"))

        webhook_url = state.get("webhook_url")
        if webhook_url:
            from services.webhook import send_scan_summary
            await send_scan_summary(webhook_url, job_id, state)
    except Exception as e:
        from tools.credential_scrubber import CredentialScrubber
        error_msg = CredentialScrubber.scrub_text(str(e))
        await redis_service.publish_log(job_id, f"Pipeline error during approval resume: {error_msg}", "error")
        await redis_service.set_job_state(job_id, {**state, "status": "FAILED", "error": error_msg})
        mark_job_failed(job_id, error_msg)
        record_job_outcome("FAILED")


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


@router.get("/{job_id}/logs")
async def stream_logs(job_id: str):
    """SSE endpoint streaming live agent log lines."""
    async def event_generator():
        async for log_msg in redis_service.subscribe_logs(job_id):
            yield {"event": "message", "data": log_msg}

    return EventSourceResponse(event_generator())
