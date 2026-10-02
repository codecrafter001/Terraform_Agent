"""Application Rollback Workflow for deployment mode (design doc §5.1).

Supports zero-downtime rollbacks for:
- static_site: Switches CloudFront origin_path to a previous release prefix (releases/<release_id>/).
- lambda_http: Re-renders the Lambda function configuration with a previous build package/SHA and routes the "live" alias.
"""

import json
import logging
import re
from typing import Any, Dict, Optional

from deploy.artifacts import get_artifact_store
from deploy.renderer import TFVARS_FILENAME
from deploy.store import (
    DeployStatus,
    DeploymentNotFound,
    get_deployment,
    list_deployment_build_artifacts,
    transition,
    update_fields,
)

logger = logging.getLogger("terraagent.deploy.rollback")


def _can_rollback(dep: Dict[str, Any]) -> bool:
    return DeployStatus(dep.get("status", "")) in (
        DeployStatus.DEPLOYED,
        DeployStatus.MERGED,
        DeployStatus.FAILED_PARTIAL,
        DeployStatus.NEEDS_RECONCILIATION,
    ) and bool(dep.get("rendered")) and bool(dep.get("target_id"))


async def prepare_rollback(
    deployment_id: str,
    target_artifact_id: Optional[str] = None,
    target_release_id: Optional[str] = None,
    actor: str = "operator",
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Prepares and transitions a deployment for rollback to a prior build/release."""
    dep = get_deployment(deployment_id)
    if not dep:
        raise DeploymentNotFound(f"Deployment {deployment_id} not found")

    if not _can_rollback(dep):
        raise ValueError(
            f"Deployment {deployment_id} is in status '{dep.get('status')}'; rollback requires a deployed/reconciliation state with target_id."
        )

    target_type = dep.get("target_type")
    rendered = dict(dep.get("rendered") or {})
    if not rendered or TFVARS_FILENAME not in rendered:
        raise ValueError(f"Deployment {deployment_id} does not contain rendered {TFVARS_FILENAME}")

    current_tfvars = json.loads(rendered[TFVARS_FILENAME])
    new_release_id = target_release_id

    # If an artifact ID is supplied, check store
    if target_artifact_id:
        store = get_artifact_store()
        try:
            artifact_data = store.read_bytes(target_artifact_id)
        except Exception as e:
            raise ValueError(f"Could not load target artifact '{target_artifact_id}': {e}")

        # Compute SHA from artifact if needed
        import hashlib
        artifact_sha = hashlib.sha256(artifact_data).hexdigest()
        if not new_release_id:
            new_release_id = artifact_sha[:12]

    # If no explicit release/artifact provided, pick the previous one from history
    if not new_release_id and not target_artifact_id:
        history = list_deployment_build_artifacts(deployment_id, limit=5)
        if len(history) > 1:
            # First item is latest, second is previous
            prev = history[1]
            target_artifact_id = prev["artifact_id"]
            new_release_id = prev.get("release_id", prev["sha256"][:12])
        elif len(history) == 1:
            new_release_id = history[0].get("release_id", "v1")
        else:
            new_release_id = "v1"

    if target_type == "static_site":
        clean_release = re.sub(r"[^a-zA-Z0-9_-]", "", new_release_id or "v1")
        current_tfvars["release_id"] = clean_release
        settings = dict(dep.get("settings") or {})
        settings["release_id"] = clean_release
        update_fields(deployment_id, settings=settings)

    elif target_type == "lambda_http":
        if target_artifact_id:
            # If target artifact is given, update package hash
            current_tfvars["package_file"] = f"artifacts/{target_artifact_id}.zip"
            import base64
            current_tfvars["package_sha256_b64"] = base64.b64encode(hashlib.sha256(artifact_data).digest()).decode("ascii")

    # Update rendered files with the new tfvars
    rendered[TFVARS_FILENAME] = json.dumps(current_tfvars, indent=2, sort_keys=True) + "\n"

    rollback_reason = reason or f"Rollback requested to release '{new_release_id or target_artifact_id}'"

    # Transition deployment to PLANNING to run a fresh plan with the rollback parameters
    transition(
        deployment_id,
        DeployStatus.PLANNING,
        actor=actor,
        reason=rollback_reason,
        allowed_from=frozenset({DeployStatus.DEPLOYED, DeployStatus.MERGED, DeployStatus.FAILED_PARTIAL, DeployStatus.NEEDS_RECONCILIATION}),
        rendered=rendered,
        target_id=dep["target_id"],
        plan=None,
        plan_summary=None,
        plan_bundle_sha256=None,
        plan_artifact_id=None,
        plan_policy=None,
        is_destructive=False,
        approved_by=None,
        approved_at=None,
        applied_at=None,
        error=None,
    )

    logger.info(f"[{deployment_id}] Prepared rollback to release '{new_release_id or target_artifact_id}'. Initiating plan.")
    return {
        "deployment_id": deployment_id,
        "status": DeployStatus.PLANNING.value,
        "release_id": new_release_id,
        "artifact_id": target_artifact_id,
        "message": f"Rollback initiated. Planning changes against target '{dep['target_id']}'.",
    }
