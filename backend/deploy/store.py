"""Deployment records and the deployment state machine (design doc §3.1).

`transition()` is the only writer of deployments.status. It checks the move
against TRANSITIONS under a row lock and appends a DeploymentEvent in the same
transaction, so the audit trail can't drift from the record. Phases 1-2 only
have the pre-apply states; later phases extend the table, they don't bypass it.
"""

import json
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

from deploy.artifacts import StoredArtifact
from services.database import SessionLocal

# Stored as <name>_json columns, returned decoded under <name>.
_JSON_FIELDS = (
    "intake",
    "profile",
    "decision",
    "settings",
    "build",
    "rendered",
    "verification",
    "plan",
    "plan_summary",
    "plan_policy",
    "outputs",
    "pr",
    "code_update",
)


class DeployStatus(str, Enum):
    SOURCE_RECEIVED = "SOURCE_RECEIVED"
    ANALYZING = "ANALYZING"
    ANALYZED = "ANALYZED"
    BUILDING = "BUILDING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    PR_OPEN = "PR_OPEN"
    MERGED = "MERGED"
    APPLYING = "APPLYING"
    DEPLOYED = "DEPLOYED"
    DESTROY_PLANNING = "DESTROY_PLANNING"
    DESTROYING = "DESTROYING"
    DESTROYED = "DESTROYED"
    FAILED_PARTIAL = "FAILED_PARTIAL"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


S = DeployStatus
TRANSITIONS: Dict[DeployStatus, frozenset] = {
    S.SOURCE_RECEIVED: frozenset({S.ANALYZING, S.FAILED}),
    S.ANALYZING: frozenset({S.ANALYZED, S.FAILED}),
    S.ANALYZED: frozenset({S.BUILDING, S.FAILED}),
    S.BUILDING: frozenset({S.VERIFYING, S.FAILED}),
    S.VERIFYING: frozenset({S.VERIFIED, S.FAILED}),
    S.VERIFIED: frozenset({S.BUILDING, S.PLANNING}),
    S.PLANNING: frozenset({S.AWAITING_APPROVAL, S.FAILED}),
    S.DESTROY_PLANNING: frozenset({S.AWAITING_APPROVAL, S.FAILED}),
    S.AWAITING_APPROVAL: frozenset({S.APPROVED, S.REJECTED, S.EXPIRED}),
    S.APPROVED: frozenset({S.APPLYING, S.DESTROYING, S.PLANNING, S.EXPIRED, S.PR_OPEN}),
    S.PR_OPEN: frozenset({S.MERGED, S.FAILED, S.PLANNING}),
    S.MERGED: frozenset({S.PLANNING}),
    S.APPLYING: frozenset({S.DEPLOYED, S.FAILED_PARTIAL, S.NEEDS_RECONCILIATION}),
    S.DESTROYING: frozenset({S.DESTROYED, S.FAILED_PARTIAL, S.NEEDS_RECONCILIATION, S.FAILED}),
    S.DEPLOYED: frozenset({S.PLANNING, S.DESTROY_PLANNING, S.SOURCE_RECEIVED}),
    S.DESTROYED: frozenset(),
    S.FAILED_PARTIAL: frozenset({S.PLANNING, S.DESTROY_PLANNING}),
    S.NEEDS_RECONCILIATION: frozenset({S.PLANNING, S.DESTROY_PLANNING}),
    S.REJECTED: frozenset({S.BUILDING, S.PLANNING, S.SOURCE_RECEIVED, S.DESTROY_PLANNING}),
    S.EXPIRED: frozenset({S.PLANNING, S.SOURCE_RECEIVED, S.DESTROY_PLANNING}),
    S.FAILED: frozenset({S.BUILDING, S.PLANNING, S.SOURCE_RECEIVED, S.DESTROY_PLANNING}),
}
IN_PROGRESS = frozenset({S.SOURCE_RECEIVED, S.ANALYZING, S.BUILDING, S.VERIFYING, S.PLANNING, S.APPLYING, S.DESTROY_PLANNING, S.DESTROYING})
TERMINAL = frozenset({S.ANALYZED, S.DEPLOYED, S.DESTROYED, S.MERGED, S.FAILED_PARTIAL, S.NEEDS_RECONCILIATION, S.REJECTED, S.EXPIRED, S.FAILED})


# A deployed stack can always be torn down from these...
DESTROYABLE = frozenset({S.DEPLOYED, S.FAILED_PARTIAL, S.NEEDS_RECONCILIATION})
# ...and from these when it was applied at some point (a code update failed, was
# rejected or its approval expired, or a destroy plan itself failed).
DESTROYABLE_IF_APPLIED = frozenset({S.FAILED, S.REJECTED, S.EXPIRED})


def can_destroy(dep: Dict[str, Any]) -> bool:
    status = DeployStatus(dep["status"])
    return status in DESTROYABLE or (status in DESTROYABLE_IF_APPLIED and bool(dep.get("applied_at")))


class InvalidTransition(Exception):
    pass


class DeploymentNotFound(Exception):
    pass


def _now() -> str:
    return datetime.utcnow().isoformat()


def new_deployment_id() -> str:
    return f"dep-{uuid.uuid4().hex[:12]}"


def _to_dict(rec: Any) -> Dict[str, Any]:
    out = {
        "id": rec.id,
        "status": rec.status,
        "source_kind": rec.source_kind,
        "source_name": rec.source_name,
        "source_sha256": rec.source_sha256,
        "source_artifact_id": rec.source_artifact_id,
        "region": rec.region,
        "environment": rec.environment,
        "requested_by": rec.requested_by,
        "owner": getattr(rec, "owner", None),
        "tenant_id": getattr(rec, "tenant_id", None),
        "target_type": rec.target_type,
        "target_id": getattr(rec, "target_id", None),
        "plan_bundle_sha256": getattr(rec, "plan_bundle_sha256", None),
        "plan_artifact_id": getattr(rec, "plan_artifact_id", None),
        "plan_kind": getattr(rec, "plan_kind", "plan") or "plan",
        "is_destructive": getattr(rec, "is_destructive", False),
        "approved_by": getattr(rec, "approved_by", None),
        "approved_at": getattr(rec, "approved_at", None),
        "approval_reason": getattr(rec, "approval_reason", None),
        "rejection_reason": getattr(rec, "rejection_reason", None),
        "applied_at": getattr(rec, "applied_at", None),
        "verdict": rec.verdict,
        "error": rec.error,
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
        "completed_at": rec.completed_at,
    }
    for name in _JSON_FIELDS:
        raw = getattr(rec, f"{name}_json", None)
        out[name] = json.loads(raw) if raw else None
    return out


def _set_fields(rec: Any, fields: Dict[str, Any]) -> None:
    for key, value in fields.items():
        if key in _JSON_FIELDS:
            setattr(rec, f"{key}_json", None if value is None else json.dumps(value))
        elif key in (
            "target_type",
            "target_id",
            "verdict",
            "error",
            "source_name",
            "source_sha256",
            "source_artifact_id",
            "plan_bundle_sha256",
            "plan_artifact_id",
            "plan_kind",
            "is_destructive",
            "approved_by",
            "approved_at",
            "approval_reason",
            "rejection_reason",
            "applied_at",
            "completed_at",
            "owner",
            "tenant_id",
        ):
            setattr(rec, key, value)
        else:
            raise ValueError(f"unknown deployment field '{key}'")



def create_deployment(
    deployment_id: str,
    source_kind: str,
    source_name: str,
    region: str,
    environment: str,
    requested_by: Optional[str] = None,
    owner: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    from models.orm import Deployment, DeploymentEvent

    now = _now()
    user_owner = owner or requested_by
    session = SessionLocal()
    try:
        rec = Deployment(
            id=deployment_id,
            status=S.SOURCE_RECEIVED.value,
            source_kind=source_kind,
            source_name=source_name[:300],
            region=region,
            environment=environment,
            requested_by=requested_by or user_owner,
            owner=user_owner,
            tenant_id=tenant_id,
            created_at=now,
            updated_at=now,
        )
        session.add(rec)
        session.add(DeploymentEvent(deployment_id=deployment_id, from_status=None, to_status=S.SOURCE_RECEIVED.value,
                                    actor=requested_by or user_owner or "api", reason=f"{source_kind} source received", created_at=now))
        session.commit()
        return _to_dict(rec)
    finally:
        session.close()


def transition(deployment_id: str, to: DeployStatus, actor: str = "system", reason: Optional[str] = None,
               allowed_from: Optional[frozenset] = None, **fields: Any) -> Dict[str, Any]:
    """Move to `to` (and set `fields`) if TRANSITIONS allows it from the
    current status - and, when given, only from a status in `allowed_from`."""
    from models.orm import Deployment, DeploymentEvent

    session = SessionLocal()
    try:
        rec = session.query(Deployment).filter(Deployment.id == deployment_id).with_for_update().one_or_none()
        if rec is None:
            raise DeploymentNotFound(deployment_id)
        current = DeployStatus(rec.status)
        if to not in TRANSITIONS.get(current, frozenset()) or (allowed_from is not None and current not in allowed_from):
            raise InvalidTransition(f"{deployment_id}: {current.value} -> {to.value} is not allowed")
        now = _now()
        rec.status = to.value
        rec.updated_at = now
        if to in TERMINAL:
            fields.setdefault("completed_at", now)
        elif "completed_at" not in fields:
            rec.completed_at = None
        _set_fields(rec, fields)
        session.add(DeploymentEvent(deployment_id=deployment_id, from_status=current.value, to_status=to.value,
                                    actor=actor, reason=(reason or "")[:2000] or None, created_at=now))
        session.commit()
        return _to_dict(rec)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def update_fields(deployment_id: str, **fields: Any) -> None:
    """Set data fields without a status change (e.g. the source artifact)."""
    from models.orm import Deployment

    session = SessionLocal()
    try:
        rec = session.get(Deployment, deployment_id)
        if rec is None:
            raise DeploymentNotFound(deployment_id)
        _set_fields(rec, fields)
        rec.updated_at = _now()
        session.commit()
    finally:
        session.close()


def get_deployment(deployment_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    from models.orm import Deployment

    session = SessionLocal()
    try:
        rec = session.get(Deployment, deployment_id)
        if not rec:
            return None
        if tenant_id and rec.tenant_id and rec.tenant_id != tenant_id:
            return None
        return _to_dict(rec)
    finally:
        session.close()


def list_deployments(limit: int = 50, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    from models.orm import Deployment

    session = SessionLocal()
    try:
        query = session.query(Deployment)
        if tenant_id:
            query = query.filter(Deployment.tenant_id == tenant_id)
        rows = query.order_by(Deployment.created_at.desc()).limit(limit).all()
        return [_to_dict(r) for r in rows]
    finally:
        session.close()


def list_events(deployment_id: str, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    from models.orm import Deployment, DeploymentEvent

    session = SessionLocal()
    try:
        if tenant_id:
            dep = session.get(Deployment, deployment_id)
            if not dep or (dep.tenant_id and dep.tenant_id != tenant_id):
                return []
        rows = (session.query(DeploymentEvent).filter(DeploymentEvent.deployment_id == deployment_id)
                .order_by(DeploymentEvent.id).all())
        return [{"from_status": r.from_status, "to_status": r.to_status, "actor": r.actor, "reason": r.reason,
                 "created_at": r.created_at} for r in rows]
    finally:
        session.close()


def record_artifact(deployment_id: str, kind: str, stored: StoredArtifact, sensitive: bool,
                    retention_days: Optional[int]) -> None:
    from models.orm import DeploymentArtifact

    now = datetime.utcnow()
    session = SessionLocal()
    try:
        session.add(DeploymentArtifact(
            id=stored["artifact_id"], deployment_id=deployment_id, kind=kind, sha256=stored["sha256"],
            size=stored["size"], sensitive=sensitive, created_at=now.isoformat(),
            expires_at=(now + timedelta(days=retention_days)).isoformat() if retention_days else None,
        ))
        session.commit()
    finally:
        session.close()


def expired_artifacts(now: Optional[datetime] = None) -> List[str]:
    from models.orm import DeploymentArtifact

    cutoff = (now or datetime.utcnow()).isoformat()
    session = SessionLocal()
    try:
        rows = session.query(DeploymentArtifact.id).filter(
            DeploymentArtifact.expires_at.isnot(None), DeploymentArtifact.expires_at < cutoff).all()
        return [r[0] for r in rows]
    finally:
        session.close()


def delete_artifact_record(artifact_id: str) -> None:
    from models.orm import DeploymentArtifact

    session = SessionLocal()
    try:
        session.query(DeploymentArtifact).filter(DeploymentArtifact.id == artifact_id).delete()
        session.commit()
    finally:
        session.close()


def stale_in_progress(max_age_seconds: int, now: Optional[datetime] = None) -> List[str]:
    """Deployments stuck in a pre-apply working state (their worker is gone)."""
    from models.orm import Deployment

    cutoff = ((now or datetime.utcnow()) - timedelta(seconds=max_age_seconds)).isoformat()
    session = SessionLocal()
    try:
        rows = session.query(Deployment.id).filter(
            Deployment.status.in_([s.value for s in IN_PROGRESS]), Deployment.updated_at < cutoff).all()
        return [r[0] for r in rows]
    finally:
        session.close()


def list_deployment_build_artifacts(deployment_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Lists the recent build/bundle artifacts for this deployment to support rollbacks."""
    from models.orm import DeploymentArtifact

    session = SessionLocal()
    try:
        rows = (
            session.query(DeploymentArtifact)
            .filter(
                DeploymentArtifact.deployment_id == deployment_id,
                DeploymentArtifact.kind.in_(["bundle", "build", "source"]),
            )
            .order_by(DeploymentArtifact.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "artifact_id": r.id,
                "deployment_id": r.deployment_id,
                "kind": r.kind,
                "sha256": r.sha256,
                "size": r.size,
                "created_at": r.created_at,
                "release_id": r.sha256[:12] if r.sha256 else "initial",
            }
            for r in rows
        ]
    finally:
        session.close()

