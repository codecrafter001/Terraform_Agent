"""Celery tasks for deployment mode, routed to the `deploy_plan` queue
(services/celery_app.py). Every task takes only a deployment id: no
credentials, tokens or source content ever travel through the broker."""

import asyncio
import logging
import sys

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


from services.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="deploy.analyze")
def analyze_task(deployment_id: str) -> None:
    from deploy.pipeline import run_analysis

    asyncio.run(run_analysis(deployment_id))


@celery_app.task(name="deploy.build_and_verify")
def build_and_verify_task(deployment_id: str) -> None:
    from deploy.pipeline import run_build_and_verify

    asyncio.run(run_build_and_verify(deployment_id))


@celery_app.task(name="deploy.plan")
def plan_task(deployment_id: str) -> None:
    from deploy.pipeline import run_plan

    asyncio.run(run_plan(deployment_id))


@celery_app.task(name="deploy.plan_destroy")
def plan_destroy_task(deployment_id: str) -> None:
    from deploy.apply_runner import plan_destroy

    asyncio.run(plan_destroy(deployment_id))


@celery_app.task(name="deploy.apply", acks_late=False, max_retries=0, soft_time_limit=2520)
def apply_task(deployment_id: str) -> None:
    from deploy.apply_runner import apply_approved

    asyncio.run(apply_approved(deployment_id, routing_key="deploy_apply"))


@celery_app.task(name="deploy.sandbox_suite")
def sandbox_suite_task() -> dict:
    """Scheduled end-to-end test suite against sandbox account."""
    from deploy.sandbox_runner import run_sandbox_suite

    return asyncio.run(run_sandbox_suite())



@celery_app.task(name="deploy.sweep")
def sweep_task() -> dict:
    """Fail deployments whose worker disappeared mid-stage (safe: nothing
    before apply changes AWS), sweep APPLYING deployments past lease to NEEDS_RECONCILIATION,
    expire approvals past 24h, and delete expired artifacts."""
    from datetime import datetime, timedelta
    from deploy.artifacts import get_artifact_store
    from deploy.config import MAX_STAGE_RUNTIME_SECONDS
    from deploy.store import (
        DeployStatus,
        InvalidTransition,
        delete_artifact_record,
        expired_artifacts,
        get_deployment,
        stale_in_progress,
        transition,
    )
    from models.orm import Deployment
    from services.database import SessionLocal

    failed = 0
    reconciliation_count = 0
    session = SessionLocal()
    try:
        # Check APPLYING past lease runtime (e.g. 2520s) -> NEEDS_RECONCILIATION
        cutoff_applying = (datetime.utcnow() - timedelta(seconds=2520)).isoformat()
        stale_applying = session.query(Deployment).filter(
            Deployment.status == DeployStatus.APPLYING.value,
            Deployment.updated_at < cutoff_applying,
        ).all()
        for dep in stale_applying:
            try:
                transition(
                    dep.id,
                    DeployStatus.NEEDS_RECONCILIATION,
                    actor="system",
                    reason="Apply task timed out or worker crashed during apply. State reconciliation required.",
                    error="Apply exceeded maximum duration without reporting completion. Inspect Terraform state lock manually.",
                )
                reconciliation_count += 1
            except Exception as e:
                logger.warning(f"Failed to reconcile stale applying deployment {dep.id}: {e}")
    finally:
        session.close()

    for deployment_id in stale_in_progress(MAX_STAGE_RUNTIME_SECONDS):
        try:
            dep_data = get_deployment(deployment_id)
            if dep_data and dep_data.get("status") == DeployStatus.APPLYING.value:
                continue
            transition(deployment_id, DeployStatus.FAILED, reason="worker stopped responding",
                       error="The worker stopped before this stage finished. Nothing was deployed; you can retry.")
            failed += 1
        except InvalidTransition:
            pass

    # Expire AWAITING_APPROVAL deployments past 24h
    expired_count = 0
    cutoff_24h = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    session = SessionLocal()
    store = get_artifact_store()
    try:
        stale_approvals = session.query(Deployment).filter(
            Deployment.status == DeployStatus.AWAITING_APPROVAL.value,
            Deployment.updated_at < cutoff_24h,
        ).all()
        for dep in stale_approvals:
            dep_id = dep.id
            plan_art_id = dep.plan_artifact_id
            try:
                transition(dep_id, DeployStatus.EXPIRED, actor="system", reason="Approval window expired after 24 hours")
                expired_count += 1
                if plan_art_id:
                    store.delete(plan_art_id)
                    delete_artifact_record(plan_art_id)
            except Exception as e:
                logger.warning(f"Failed to expire deployment {dep_id}: {e}")
    finally:
        session.close()

    deleted = 0
    for artifact_id in expired_artifacts():
        store.delete(artifact_id)
        delete_artifact_record(artifact_id)
        deleted += 1
    if failed or deleted or expired_count or reconciliation_count:
        logger.info(
            f"Deployment sweep: {failed} stale failed, {reconciliation_count} needs reconciliation, "
            f"{expired_count} expired approvals, {deleted} artifacts deleted"
        )
    return {
        "failed": failed,
        "needs_reconciliation": reconciliation_count,
        "expired_approvals": expired_count,
        "artifacts_deleted": deleted,
    }

