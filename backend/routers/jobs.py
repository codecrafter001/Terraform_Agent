"""Router for listing historic scan jobs and audit logs."""

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query

from models.audit_log import AuditLogRecord
from services.auth import require_api_key
from services.database import get_job_record, list_job_records
from services.redis_client import redis_service

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(require_api_key)])


def _to_audit_record(r: Any) -> AuditLogRecord:
    return AuditLogRecord(
        job_id=r.job_id,
        operation=r.operation,
        region=r.region,
        aws_account_id=r.aws_account_id,
        resources_discovered=r.resources_discovered,
        agents_completed=r.agents_completed,
        status=r.status,
        validation_passed=r.validation_passed,
        security_findings_count=r.security_findings_count,
        zip_generated=r.zip_generated,
        created_at=r.created_at,
        completed_at=r.completed_at
    )


@router.get("", response_model=List[AuditLogRecord])
async def list_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0)
):
    """Lists past scan jobs, paginated (credentials completely scrubbed/omitted)."""
    records = list_job_records(limit=limit, offset=offset)
    return [_to_audit_record(r) for r in records]


@router.get("/{job_id}")
async def get_job_detail(job_id: str) -> Dict[str, Any]:
    """Full detail for a single job: the audit record plus per-agent timing
    (agent_timings is tracked in the transient Redis job state, not the
    permanent DB audit record, since it's execution telemetry rather than a
    durable audit fact)."""
    record = get_job_record(job_id)
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")

    redis_state = await redis_service.get_job_state(job_id)
    audit = _to_audit_record(record)

    # error was only ever read from transient Redis state, never falling back to
    # record.error (the DB-persisted field) - if Redis state has expired/is
    # unavailable, a failed job's actual error message was silently lost even
    # though it's sitting right there in the database.
    redis_error = redis_state.get("error") if redis_state else None
    return {
        **audit.model_dump(),
        "agent_timings": redis_state.get("agent_timings", {}) if redis_state else {},
        "error": redis_error or record.error
    }
