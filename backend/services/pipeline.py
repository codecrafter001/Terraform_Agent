"""Runs the four-agent graph for a job - a fresh scan or a resume after a
human decision - shared by the Celery tasks and the inline (no-broker) path.

A run either finishes (COMPLETE / REJECTED) or pauses at the Delivery &
Approval Agent's interrupt(). On a pause the thread is checkpointed
(services/checkpoints.py, credentials removed) and the job is set to
AWAITING_APPROVAL with the gate's request attached. resume_pipeline() loads
that checkpoint and continues the same run with Command(resume=decision).
"""

import logging
from typing import Any, Dict, Optional

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from services.checkpoints import delete_checkpoint, export_thread, import_thread, load_checkpoint, save_checkpoint
from services.database import mark_job_complete, mark_job_failed
from services.redis_client import redis_service
from tools.credential_scrubber import CredentialScrubber

logger = logging.getLogger("terraagent.pipeline")

RECURSION_LIMIT = 50


class CheckpointMissing(Exception):
    """The paused run can't be resumed - its checkpoint is gone."""


async def _target_account_credentials(job_id: str, saver: InMemorySaver, creds: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A run that scanned through role_arn must re-verify in that same target
    account: exchange the re-supplied keys for the role's temporary ones,
    exactly as discovery did. None (-> checks can't be redone, INCOMPLETE)
    if the role can't be assumed."""
    import asyncio

    from agents.graph import build_graph
    from tools.sts_helper import assume_role

    values = (await build_graph(checkpointer=saver).aget_state(_config(job_id))).values
    role_arn = values.get("role_arn")
    if not role_arn:
        return creds
    try:
        ak, sk, token = await asyncio.to_thread(
            assume_role, role_arn, creds.get("access_key"), creds.get("secret_key"), creds.get("session_token"),
            values.get("region", "us-east-1"), f"terraagent-{job_id}-resume",
            values.get("aws_endpoint_url"), values.get("external_id"),
        )
    except Exception as e:
        logger.warning(f"[{job_id}] Could not assume {role_arn} for re-verification: "
                       f"{CredentialScrubber.scrub_text(str(e))}")
        return None
    return {"access_key": ak, "secret_key": sk, "session_token": token}


def _config(job_id: str) -> Dict[str, Any]:
    return {"configurable": {"thread_id": job_id}, "recursion_limit": RECURSION_LIMIT}


async def _run(job_id: str, saver: InMemorySaver, graph_input: Any) -> Dict[str, Any]:
    from agents.graph import build_graph
    from routers.metrics import record_job_outcome

    app = build_graph(checkpointer=saver)
    config = _config(job_id)
    try:
        await app.ainvoke(graph_input, config)
        snapshot = await app.aget_state(config)
    except Exception as e:
        # cloud_discovery holds raw AWS credentials - scrub before the message
        # is published, stored in Postgres or served back by the API.
        error_msg = CredentialScrubber.scrub_text(str(e))
        logger.error(f"[{job_id}] Pipeline execution failed: {error_msg}")
        await redis_service.publish_log(job_id, f"Pipeline error: {error_msg}", "error")
        previous = await redis_service.get_job_state(job_id) or {}
        await redis_service.set_job_state(job_id, {**previous, "job_id": job_id, "status": "FAILED", "error": error_msg})
        mark_job_failed(job_id, error_msg)
        record_job_outcome("FAILED")
        delete_checkpoint(job_id)
        return {"status": "FAILED", "job_id": job_id, "error": error_msg}

    state = dict(snapshot.values)
    interrupts = [i for task in snapshot.tasks for i in (task.interrupts or ())]
    if interrupts:
        request = interrupts[0].value or {}
        save_checkpoint(job_id, export_thread(saver, job_id))
        findings = len(request.get("findings") or [])
        review = len(request.get("review_resources") or [])
        summaries = dict(state.get("stage_summaries") or {})
        summaries["delivery"] = (
            f"Paused at the risk gate: {findings} finding(s) to approve, {review} resource(s) in Review"
        )
        state.update({
            "status": "AWAITING_APPROVAL", "current_stage": "awaiting_approval",
            "current_agent": "awaiting_approval", "approval_request": request, "stage_summaries": summaries,
        })
        await redis_service.set_job_state(job_id, state)
        mark_job_complete(job_id, state)
        await redis_service.publish_log(
            job_id,
            f"[AGENT:delivery] Risk gate: {findings} finding(s) need approval and {review} resource(s) "
            "are in Review. Paused for a human decision.",
            agent_name="delivery",
        )
        return {"status": "AWAITING_APPROVAL", "job_id": job_id}

    delete_checkpoint(job_id)
    await redis_service.set_job_state(job_id, state)
    mark_job_complete(job_id, state)
    record_job_outcome(state.get("status", "COMPLETE"), state.get("agent_timings"))
    await redis_service.publish_log(job_id, f"Scan job {job_id} finished with status: {state.get('status')}", "system")

    webhook_url = state.get("webhook_url")
    if webhook_url:
        from services.webhook import send_scan_summary
        await send_scan_summary(webhook_url, job_id, state)
    return {"status": state.get("status"), "job_id": job_id}


async def run_pipeline(job_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
    from agents.graph import build_initial_state

    await redis_service.publish_log(job_id, f"Initializing TerraAgent pipeline for job {job_id}...", "system")
    return await _run(job_id, InMemorySaver(), build_initial_state(job_id, request))


async def resume_pipeline(
    job_id: str, decision: Dict[str, Any], aws_credentials: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Continue a paused run with the human's decision. Re-supplied AWS
    credentials (optional) let a re-verification redo live checks; they go
    into this run's memory only and are removed again from any checkpoint."""
    data = load_checkpoint(job_id)
    if not data:
        raise CheckpointMissing(job_id)
    saver = InMemorySaver()
    import_thread(saver, job_id, data)
    if aws_credentials:
        aws_credentials = await _target_account_credentials(job_id, saver, aws_credentials)
    update = {"aws_credentials": aws_credentials} if aws_credentials else None
    return await _run(job_id, saver, Command(resume=decision, update=update))
