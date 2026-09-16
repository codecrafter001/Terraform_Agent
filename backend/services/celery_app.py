"""Celery task queue configuration and task dispatcher."""

import asyncio
import logging
import os
import time
from datetime import datetime

from celery import Celery
from celery.schedules import crontab

logger = logging.getLogger(__name__)

broker_url = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
result_backend = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/tmp/terraagent")
ZIP_EXPIRY_SECONDS = int(os.getenv("ZIP_EXPIRY_HOURS", "24")) * 3600
# Generous ceiling for a real scan (AWS discovery + LLM calls + terraform/tfsec/
# checkov/trivy subprocesses, possibly through 2 repair cycles) - only meant to
# catch a worker that died and will never report back, not a slow-but-alive one.
MAX_JOB_RUNTIME_SECONDS = int(os.getenv("MAX_JOB_RUNTIME_SECONDS", "1800"))

celery_app = Celery(
    "terraagent",
    broker=broker_url,
    backend=result_backend
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    broker_connection_retry_on_startup=False,
    broker_connection_max_retries=0,
    broker_connection_timeout=1.0,
    # Default is task_acks_late=False - a worker ACKs a task the instant it's
    # received, before execution even starts. If that worker then crashes
    # (OOM kill, container restart) mid-scan, the task is already gone and is
    # never redelivered to another worker; the job's status just stays
    # "RUNNING" forever with nothing to retry it. acks_late=True defers the ACK
    # until the task actually finishes (success or raised exception), so a
    # killed worker's in-flight task goes back on the queue for another worker
    # to pick up. prefetch_multiplier=1 pairs with this so one worker doesn't
    # hoard several long-running scans it hasn't ack'd yet, starving others.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Backstop for the case acks_late can't cover - the whole worker process
    # (not just the task) disappears without Celery ever finding out (a hard
    # host reboot, a lost container). mark_stale_jobs_failed sweeps for jobs
    # that have been "RUNNING" far longer than any real scan should take.
    beat_schedule={
        "cleanup-expired-zips": {
            "task": "cleanup_expired_zips",
            "schedule": crontab(minute=0),  # every hour on the hour
        },
        "sweep-stale-jobs": {
            "task": "sweep_stale_jobs",
            "schedule": crontab(minute="*/10"),  # every 10 minutes
        },
    },
)



@celery_app.task(name="cleanup_expired_zips")
def cleanup_expired_zips():
    """Delete generated ZIP bundles older than ZIP_EXPIRY_HOURS. Output ZIPs
    contain the full discovered inventory and generated Terraform - they
    shouldn't accumulate on disk indefinitely."""
    if not os.path.isdir(OUTPUT_DIR):
        return {"deleted": 0}

    now = time.time()
    deleted = 0
    for filename in os.listdir(OUTPUT_DIR):
        if not filename.endswith(".zip"):
            continue
        path = os.path.join(OUTPUT_DIR, filename)
        try:
            if now - os.path.getmtime(path) > ZIP_EXPIRY_SECONDS:
                os.remove(path)
                deleted += 1
        except OSError as e:
            logger.warning(f"Failed to check/delete {path}: {e}")

    if deleted:
        logger.info(f"Cleanup: removed {deleted} expired ZIP bundle(s) from {OUTPUT_DIR}")
    return {"deleted": deleted}


@celery_app.task(name="sweep_stale_jobs")
def sweep_stale_jobs():
    """Fail any job stuck "RUNNING" past MAX_JOB_RUNTIME_SECONDS - the backstop
    for a worker process that disappeared entirely (task_acks_late handles a
    task that gets redelivered; this handles the case where nothing is left
    to redeliver it to, or the whole thing just silently hung)."""
    from services.database import mark_stale_jobs_failed
    marked_failed = mark_stale_jobs_failed(MAX_JOB_RUNTIME_SECONDS)
    if marked_failed:
        logger.warning(f"Stale-job sweep: marked {marked_failed} job(s) FAILED (stuck RUNNING past {MAX_JOB_RUNTIME_SECONDS}s)")
    return {"marked_failed": marked_failed}


@celery_app.task(name="run_scan_task")
def run_scan_task(job_id: str, scan_request_dict: dict):
    """Celery background worker task that orchestrates the LangGraph pipeline."""
    from agents.graph import build_graph
    from routers.metrics import record_job_outcome
    from services.database import mark_job_complete, mark_job_failed
    from services.redis_client import redis_service

    logger.info(f"Starting Celery scan task for Job ID: {job_id}")

    async def _execute():
        # Publish initial status
        await redis_service.publish_log(job_id, f"Initializing TerraAgent pipeline for job {job_id}...", "system")

        # Build state graph
        app = build_graph()
        initial_state = {
            "job_id": job_id,
            "created_at": scan_request_dict.get("created_at", datetime.utcnow().isoformat()),
            "operation": scan_request_dict.get("operation", "generate"),
            "region": scan_request_dict.get("region", "us-east-1"),
            "resource_filters": scan_request_dict.get("resource_filters", ["EC2", "VPC", "S3", "RDS", "IAM", "SG"]),
            "aws_credentials": {
                "access_key": scan_request_dict.get("aws_access_key"),
                "secret_key": scan_request_dict.get("aws_secret_key"),
                "session_token": scan_request_dict.get("aws_session_token")
            },
            "aws_endpoint_url": scan_request_dict.get("aws_endpoint_url"),
            "role_arn": scan_request_dict.get("role_arn"),
            "webhook_url": scan_request_dict.get("webhook_url"),
            "zip_password": scan_request_dict.get("zip_password"),
            "terraform_binary": scan_request_dict.get("terraform_binary", "terraform"),
            "run_plan_equivalence": scan_request_dict.get("run_plan_equivalence", False),
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

        # Run pipeline
        try:
            final_state = await app.ainvoke(initial_state)
        except Exception as e:
            # This catches failures from cloud_discovery_node, the one node that
            # actually holds raw AWS credentials - unlike every other exception
            # boundary in this codebase (e.g. main.py's global handler), nothing
            # here scrubbed the message before it was logged/stored/returned.
            # Scrub before it ever leaves this function: it gets published live
            # over Redis pub/sub, persisted into Postgres, and served back
            # verbatim by routers/jobs.py and routers/scan.py's DB-fallback path.
            from tools.credential_scrubber import CredentialScrubber
            error_msg = CredentialScrubber.scrub_text(str(e))
            logger.error(f"[{job_id}] Pipeline execution failed: {error_msg}")
            await redis_service.publish_log(job_id, f"Pipeline error: {error_msg}", "error")
            await redis_service.set_job_state(job_id, {
                "job_id": job_id,
                "status": "FAILED",
                "error": error_msg
            })
            mark_job_failed(job_id, error_msg)
            record_job_outcome("FAILED")
            return {"status": "FAILED", "job_id": job_id, "error": error_msg}

        # Save state in Redis cache
        await redis_service.set_job_state(job_id, final_state)
        mark_job_complete(job_id, final_state)
        record_job_outcome(final_state.get("status", "COMPLETE"), final_state.get("agent_timings"))
        await redis_service.publish_log(job_id, f"Scan job {job_id} completed with status: {final_state.get('status')}", "system")

        webhook_url = final_state.get("webhook_url")
        if webhook_url:
            from services.webhook import send_scan_summary
            await send_scan_summary(webhook_url, job_id, final_state)

        return {"status": final_state.get("status"), "job_id": job_id}

    return asyncio.run(_execute())
