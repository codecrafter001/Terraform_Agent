"""Code updates for an already-deployed stack (docs/design/fast-deploy-and-service-selection.md, #5).

New source for a deployed app reuses the same deployment: same id (so the same
Terraform state and resource names), target, settings and AWS account. The
pipeline then runs analyze -> build -> verify -> plan on its own and stops at
AWAITING_APPROVAL: a human still approves the exact plan before anything is
applied (CLAUDE.md rule #1), and apply still only happens through apply_approved.

What makes it fast:
- verification is reused when the rendered .tf files are unchanged (a code
  change only changes terraform.tfvars.json: the image tag), since the scanners
  only read .tf files;
- for container targets the plan is a handful of in-place changes (the new
  source.zip, CodeBuild's IMAGE_TAG, a new task-definition revision, the
  service), applied in seconds; the build pipeline then rolls the app.
"""

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from deploy.store import DeployStatus, transition

# Resource address -> plan actions that still count as "only the code changed".
# A task-definition change is always a replace in Terraform, but ECS keeps it as a
# new revision; the running tasks are drained only once the new ones are healthy.
CODE_ONLY_CHANGES: Dict[str, frozenset] = {
    "aws_s3_object.source": frozenset({"update", "create"}),
    "aws_codebuild_project.builder": frozenset({"update"}),
    "aws_ecs_task_definition.app": frozenset({"update", "replace"}),
    "aws_ecs_service.app": frozenset({"update"}),
    "aws_ecs_task_definition.worker[0]": frozenset({"update", "replace"}),
    "aws_ecs_service.worker[0]": frozenset({"update"}),
}

# A code update may start from a deployed stack, or retry after the update itself
# failed, was rejected or its approval expired (applied_at shows a stack exists).
_UPDATABLE = frozenset({DeployStatus.DEPLOYED, DeployStatus.FAILED, DeployStatus.REJECTED, DeployStatus.EXPIRED})


def can_update_code(dep: Dict[str, Any]) -> bool:
    status = DeployStatus(dep["status"])
    if status not in _UPDATABLE or not dep.get("target_id") or not dep.get("target_type"):
        return False
    return status == DeployStatus.DEPLOYED or bool(dep.get("applied_at"))


def is_active(dep: Optional[Dict[str, Any]]) -> bool:
    return bool(dep and (dep.get("code_update") or {}).get("active"))


def tf_fingerprint(rendered: Optional[Dict[str, str]]) -> Optional[str]:
    """SHA-256 over the rendered .tf files only (not terraform.tfvars.json)."""
    tf = {name: content for name, content in (rendered or {}).items() if name.endswith(".tf")}
    if not tf:
        return None
    digest = hashlib.sha256()
    for name in sorted(tf):
        digest.update(name.encode() + b"\0" + tf[name].encode() + b"\0")
    return digest.hexdigest()


def reusable_verification(dep: Dict[str, Any], rendered: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """The previous release's verification, when the .tf files it checked are
    byte-for-byte what was just rendered and it didn't fail."""
    update = dep.get("code_update") or {}
    previous = update.get("previous_verification")
    if not previous or previous.get("verdict") == "FAIL":
        return None
    if not update.get("previous_tf_sha256") or update["previous_tf_sha256"] != tf_fingerprint(rendered):
        return None
    return {**previous, "reused_from_previous_release": True}


def is_code_only(changes: List[Dict[str, Any]]) -> bool:
    """True when every planned change is one a code update makes (and there is at least one)."""
    if not changes:
        return False
    return all(c.get("action") in CODE_ONLY_CHANGES.get(c.get("address", ""), frozenset()) for c in changes)


def start(dep: Dict[str, Any], artifact: Dict[str, Any], source_name: str, actor: str) -> Dict[str, Any]:
    """Moves a deployed stack to SOURCE_RECEIVED with the new source, keeping its
    target, settings, AWS account and outputs, and clearing the previous plan."""
    now = datetime.now(timezone.utc).isoformat()
    return transition(
        dep["id"], DeployStatus.SOURCE_RECEIVED, actor=actor,
        reason=f"code update: new source {source_name}",
        allowed_from=_UPDATABLE,
        source_name=source_name[:300],
        source_artifact_id=artifact["artifact_id"],
        source_sha256=artifact["sha256"],
        code_update={
            "active": True,
            "started_at": now,
            "requested_by": actor,
            "previous_source_sha256": dep.get("source_sha256"),
            "previous_image_tag": (dep.get("build") or {}).get("image_tag"),
            "previous_tf_sha256": tf_fingerprint(dep.get("rendered")),
            "previous_verification": dep.get("verification"),
        },
        plan=None, plan_summary=None, plan_policy=None, plan_bundle_sha256=None, plan_artifact_id=None,
        plan_kind="plan", is_destructive=False, approved_by=None, approved_at=None, approval_reason=None,
        rejection_reason=None, error=None,
    )
