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


class Deployment(Base):
    """Deployment mode (deploy/, docs/design/code-to-aws-deployment.md).
    status changes only through deploy/store.py::transition, which also writes
    a DeploymentEvent. *_json columns hold JSON; none ever holds credentials
    or tokens."""
    __tablename__ = "deployments"

    id = Column(String, primary_key=True, index=True)
    status = Column(String, nullable=False)
    source_kind = Column(String, nullable=False)  # zip | github
    source_name = Column(String, nullable=False)  # upload filename, or owner/repo@ref
    source_sha256 = Column(String, nullable=True)
    source_artifact_id = Column(String, nullable=True)
    region = Column(String, nullable=False)
    environment = Column(String, nullable=False)
    requested_by = Column(String, nullable=True)
    intake_json = Column(Text, nullable=True)  # files kept/dropped, secret-scan hits (path/line/kind only)
    profile_json = Column(Text, nullable=True)  # deploy/analyzer.py::ProjectProfile
    decision_json = Column(Text, nullable=True)  # deploy/decision_engine.py::Decision
    target_type = Column(String, nullable=True)
    settings_json = Column(Text, nullable=True)
    build_json = Column(Text, nullable=True)
    rendered_json = Column(Text, nullable=True)  # rendered .tf files + terraform.tfvars.json
    verification_json = Column(Text, nullable=True)
    verdict = Column(String, nullable=True)  # PASS | INCOMPLETE | FAIL
    # Phase 3 additions (Target, Plan, Policy, Approval)
    target_id = Column(String, nullable=True)
    plan_json = Column(Text, nullable=True)  # Redacted plan JSON
    plan_summary_json = Column(Text, nullable=True)  # {counts: {...}, changes: [...], is_destructive: bool}
    plan_bundle_sha256 = Column(String, nullable=True)
    plan_artifact_id = Column(String, nullable=True)
    plan_policy_json = Column(Text, nullable=True)  # OPA / plan policy evaluation
    plan_kind = Column(String, default="plan", nullable=True)  # "plan" | "destroy" (Phase 5.2)
    is_destructive = Column(Boolean, default=False)
    approved_by = Column(String, nullable=True)
    approved_at = Column(String, nullable=True)
    approval_reason = Column(Text, nullable=True)
    rejection_reason = Column(Text, nullable=True)
    # Phase 4 additions (Apply, Outputs, GitOps PR)
    applied_at = Column(String, nullable=True)
    outputs_json = Column(Text, nullable=True)  # JSON outputs from terraform show -json
    pr_json = Column(Text, nullable=True)  # GitOps PR details (Phase 4B)
    # Phase 6 additions (Tenancy and ownership)
    owner = Column(String, nullable=True, index=True)
    tenant_id = Column(String, nullable=True, index=True)
    error = Column(Text, nullable=True)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)
    completed_at = Column(String, nullable=True)


class AwsDeployTarget(Base):
    """AWS Account deployment target registered by the customer (Phase 3).
    Holds role ARNs, region, state bucket and the per-target ExternalId."""
    __tablename__ = "aws_deploy_targets"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    account_id = Column(String, nullable=False)
    region = Column(String, nullable=False)
    plan_role_arn = Column(String, nullable=False)
    apply_role_arn = Column(String, nullable=False)
    permissions_boundary_arn = Column(String, nullable=True)
    state_bucket = Column(String, nullable=False)
    external_id = Column(String, nullable=False)
    owner = Column(String, nullable=True, index=True)
    tenant_id = Column(String, nullable=True, index=True)
    verified_at = Column(String, nullable=True)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class DeploymentEvent(Base):
    """Append-only audit trail of a deployment's status changes."""
    __tablename__ = "deployment_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    deployment_id = Column(String, nullable=False, index=True)
    from_status = Column(String, nullable=True)
    to_status = Column(String, nullable=False)
    actor = Column(String, nullable=False)
    reason = Column(Text, nullable=True)
    created_at = Column(String, nullable=False)


class DeploymentArtifact(Base):
    """An artifact in deploy/artifacts.py's store. Sensitive artifacts (user
    code, build output, plan bundle) are never served for download."""
    __tablename__ = "deployment_artifacts"

    id = Column(String, primary_key=True)  # the store's artifact id
    deployment_id = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False)  # source | bundle | plan_binary | plan_bundle
    sha256 = Column(String, nullable=False)
    size = Column(Integer, nullable=False)
    sensitive = Column(Boolean, nullable=False, default=True)
    created_at = Column(String, nullable=False)
    expires_at = Column(String, nullable=True)

