"""SQLAlchemy engine, session factory, and job persistence helpers.

Serves as the audit-trail store described in the build plan: a lightweight
record per scan job (status, counts, timestamps) separate from the
transient, richer job state kept in Redis.
"""

import os
from datetime import datetime, timedelta
from typing import Any, Dict, List

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////tmp/terraagent/terraagent.db")

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
# pool_pre_ping: a real client-server DB (Postgres) can drop idle connections
# (a container restart, a network blip, the server's own idle timeout) - without
# this, the next query on a dead pooled connection raises OperationalError
# ("server closed the connection unexpectedly") instead of transparently
# reconnecting. SQLite has no such failure mode (no server to restart), but the
# flag is a no-op there, so it's simplest to always set it.
engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def init_db() -> None:
    """Create tables that don't exist yet, and add any columns that don't
    exist yet on tables that do. Safe to call on every startup.

    This project has no Alembic/migration tooling - JobRecord has grown new
    nullable columns several times as features shipped (classification_summary,
    adoption_risk_score, plan_equivalence_confidence), and Base.metadata
    .create_all() only creates missing TABLES - it silently does nothing for
    a column added to the model after a table already exists in a persistent
    volume. Verified live: a real deployment's jobs table, created before
    those 3 columns existed, made every single POST /api/scan fail with
    psycopg2.errors.UndefinedColumn - create_job_record's very first write
    hit it immediately. All of JobRecord's columns added post-launch have
    been simple nullable columns with no default, so a plain
    ALTER TABLE ADD COLUMN is sufficient - this is not a general-purpose
    migration system, just enough to keep create_all()'s "safe to call
    every startup" promise true for the additive case that keeps happening.
    """
    from models.orm import JobRecord  # noqa: F401  (registers model with Base)
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    if JobRecord.__tablename__ not in inspector.get_table_names():
        return
    existing_columns = {col["name"] for col in inspector.get_columns(JobRecord.__tablename__)}
    missing_columns = [c for c in JobRecord.__table__.columns if c.name not in existing_columns]
    if not missing_columns:
        return
    with engine.begin() as conn:
        for column in missing_columns:
            col_type = column.type.compile(dialect=engine.dialect)
            conn.execute(text(f'ALTER TABLE {JobRecord.__tablename__} ADD COLUMN "{column.name}" {col_type}'))


def create_job_record(job_id: str, operation: str, region: str, created_at: str) -> None:
    from models.orm import JobRecord
    session = SessionLocal()
    try:
        session.merge(JobRecord(
            job_id=job_id,
            operation=operation,
            region=region,
            status="RUNNING",
            created_at=created_at
        ))
        session.commit()
    finally:
        session.close()


def mark_job_complete(job_id: str, final_state: Dict[str, Any]) -> None:
    import json

    from models.orm import JobRecord
    session = SessionLocal()
    try:
        record = session.get(JobRecord, job_id)
        if record is None:
            return
        resources = final_state.get("resources") or []
        findings = (final_state.get("security_results") or {}).get("findings") or []
        record.resources_discovered = len(resources)
        record.agents_completed = len(final_state.get("completed_agents") or [])
        record.validation_passed = bool((final_state.get("validation_results") or {}).get("passed"))
        record.security_findings_count = len(findings)
        record.zip_generated = bool(final_state.get("zip_path"))
        record.status = final_state.get("status") or "COMPLETE"

        # Audit-trail summaries for the adoption pipeline (Phase 1 Increments 1/2/5) -
        # empty/None until those agents are built and start populating these state
        # keys with real data; seeded here now so this function doesn't need
        # revisiting per increment.
        classification_summary = (final_state.get("classification_results") or {}).get("summary")
        record.classification_summary = json.dumps(classification_summary) if classification_summary else None
        adoption_plan = final_state.get("adoption_plan") or {}
        record.adoption_risk_score = adoption_plan.get("risk_score")
        plan_equivalence = final_state.get("plan_equivalence_results") or {}
        record.plan_equivalence_confidence = plan_equivalence.get("confidence_score")
        pending_approval = final_state.get("pending_approval")
        record.pending_approval_summary = json.dumps(pending_approval) if pending_approval else None
        approval_decision = final_state.get("approval_decision")
        record.approval_decision_summary = json.dumps(approval_decision) if approval_decision else None

        record.completed_at = datetime.utcnow().isoformat()
        session.commit()
    finally:
        session.close()


def set_github_pr(job_id: str, pr_info: Dict[str, Any]) -> None:
    """Persists the resulting PR url/number (never the token that created it -
    that's extracted from a SecretStr request field inside the endpoint
    handler and discarded once github_client.create_adoption_pr returns;
    it's never passed to this function or any other persistence path)."""
    from models.orm import JobRecord
    session = SessionLocal()
    try:
        record = session.get(JobRecord, job_id)
        if record is None:
            return
        record.github_pr_url = pr_info.get("pr_url")
        record.github_pr_number = pr_info.get("pr_number")
        session.commit()
    finally:
        session.close()


def set_wave_pr(job_id: str, wave_number: int, pr_info: Dict[str, Any]) -> None:
    """Persists one wave-scoped PR's url/number/branch - same non-persistence
    guarantee for the token as set_github_pr above. A job can have several
    of these (one per wave a human has opened a PR for), so this merges into
    the existing JSON-encoded summary rather than overwriting it, keyed by
    wave number (as a string - JSON object keys are always strings)."""
    import json

    from models.orm import JobRecord
    session = SessionLocal()
    try:
        record = session.get(JobRecord, job_id)
        if record is None:
            return
        existing = json.loads(record.github_wave_prs_summary) if record.github_wave_prs_summary else {}
        existing[str(wave_number)] = pr_info
        record.github_wave_prs_summary = json.dumps(existing)
        session.commit()
    finally:
        session.close()


def mark_job_failed(job_id: str, error: str) -> None:
    from models.orm import JobRecord
    session = SessionLocal()
    try:
        record = session.get(JobRecord, job_id)
        if record is None:
            return
        record.status = "FAILED"
        record.error = error
        record.completed_at = datetime.utcnow().isoformat()
        session.commit()
    finally:
        session.close()


def mark_stale_jobs_failed(max_age_seconds: int) -> int:
    """Fail any job still "RUNNING" long after it should have finished.

    Celery's default task_acks_late=False (see celery_app.py) means a worker
    that crashes mid-scan (OOM kill, container restart) never gets its task
    retried - the job's status is simply never updated again, leaving it
    "RUNNING" in the DB/Redis forever with no way for a client to ever see it
    as failed. This is the periodic (services/celery_app.py beat schedule)
    backstop for that case.
    """
    from models.orm import JobRecord
    session = SessionLocal()
    try:
        cutoff = (datetime.utcnow() - timedelta(seconds=max_age_seconds)).isoformat()
        stale = (
            session.query(JobRecord)
            .filter(JobRecord.status == "RUNNING", JobRecord.created_at < cutoff)
            .all()
        )
        for record in stale:
            record.status = "FAILED"
            record.error = (
                f"Job exceeded the maximum allowed runtime ({max_age_seconds}s) with no "
                "completion signal - the worker most likely crashed or was killed mid-scan."
            )
            record.completed_at = datetime.utcnow().isoformat()
        if stale:
            session.commit()
        return len(stale)
    finally:
        session.close()


def list_job_records(limit: int = 100, offset: int = 0) -> List[Any]:
    from models.orm import JobRecord
    session = SessionLocal()
    try:
        return (
            session.query(JobRecord)
            .order_by(JobRecord.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
    finally:
        session.close()


def get_job_record(job_id: str) -> Any:
    from models.orm import JobRecord
    session = SessionLocal()
    try:
        return session.get(JobRecord, job_id)
    finally:
        session.close()
