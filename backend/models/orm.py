"""SQLAlchemy ORM model for persisted scan job records (audit trail)."""

from sqlalchemy import Boolean, Column, Integer, String

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
    github_wave_prs_summary = Column(String, nullable=True)  # JSON-encoded {wave_number: {pr_url, pr_number, branch}}
    created_at = Column(String, nullable=False)
    completed_at = Column(String, nullable=True)
