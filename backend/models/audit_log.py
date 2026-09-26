"""SQLAlchemy and Pydantic models for Audit Logging."""

from typing import Optional

from pydantic import BaseModel


class AuditLogRecord(BaseModel):
    job_id: str
    operation: str
    region: str
    aws_account_id: Optional[str] = None  # Last 4 characters only
    resources_discovered: int = 0
    agents_completed: int = 0
    status: str
    validation_passed: bool = False
    security_findings_count: int = 0
    zip_generated: bool = False
    created_at: str
    completed_at: Optional[str] = None
    github_pr_url: Optional[str] = None
    github_pr_number: Optional[int] = None
    github_hardening_pr_url: Optional[str] = None
    github_hardening_pr_number: Optional[int] = None
    migration_safety_score: Optional[int] = None
    migration_safety_status: Optional[str] = None
    archived: bool = False
