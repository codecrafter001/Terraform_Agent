"""SQLAlchemy ORM model for persisted scan job records (audit trail)."""

from sqlalchemy import Boolean, Column, Integer, String, Text

from services.database import Base


class JobRecord(Base):
    __tablename__ = "jobs"

    job_id = Column(String, primary_key=True, index=True)
    operation = Column(String, nullable=False)
    region = Column(String, nullable=False)
    aws_account_id = Column(String, nullable=True)
    resources_discovered = Column(Integer, default=0)
    agents_completed = Column(Integer, default=0)
    status = Column(String, nullable=False, default="PENDING")
    validation_passed = Column(Boolean, default=False)
    security_findings_count = Column(Integer, default=0)
    zip_generated = Column(Boolean, default=False)
    error = Column(String, nullable=True)
    classification_summary = Column(String, nullable=True)  # JSON-encoded category->count dict
    adoption_risk_score = Column(Integer, nullable=True)
    plan_equivalence_confidence = Column(Integer, nullable=True)
    pending_approval_summary = Column(String, nullable=True)  # JSON-encoded {reason, findings} or null
    approval_decision_summary = Column(String, nullable=True)  # JSON-encoded {decision, reason, decided_at} or null
    github_pr_url = Column(String, nullable=True)
    github_pr_number = Column(Integer, nullable=True)
    github_hardening_pr_url = Column(String, nullable=True)
    github_hardening_pr_number = Column(Integer, nullable=True)
    github_wave_prs_summary = Column(String, nullable=True)  # JSON-encoded {wave_number: {pr_url, pr_number, branch}}
    # tools/scores.py::migration_safety, kept here so the dashboard can show it
    # after the job's Redis state has expired. score is null without evidence.
    migration_safety_score = Column(Integer, nullable=True)
    migration_safety_status = Column(String, nullable=True)
    # Soft delete: an archived job is hidden from GET /jobs, never removed -
    # the audit trail stays intact.
    archived = Column(Boolean, nullable=True)
    # The natural-language request and its parsed changes (Change Requests page).
    # User-typed text: rendered as text only, never interpreted.
    user_request = Column(Text, nullable=True)
    environment = Column(String, nullable=True)
    requested_changes_summary = Column(Text, nullable=True)  # JSON list of {resource, attribute, current_value, target_value, action}
    # tools/run_summary.py: the terraform commands the job ran (Terraform Runs page).
    runs_summary = Column(Text, nullable=True)
    created_at = Column(String, nullable=False)
    completed_at = Column(String, nullable=True)


class AppSetting(Base):
    """Non-secret workspace defaults (routers/settings.py). Never credentials or tokens."""
    __tablename__ = "app_settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)  # JSON
    updated_at = Column(String, nullable=False)


class GraphCheckpoint(Base):
    """A job's LangGraph thread while it is paused at the approval gate
    (services/checkpoints.py). Credentials are removed before it is written."""
    __tablename__ = "graph_checkpoints"

    job_id = Column(String, primary_key=True)
    data = Column(Text, nullable=False)
    updated_at = Column(String, nullable=False)
