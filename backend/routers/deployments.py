"""Deployment mode API (docs/design/code-to-aws-deployment.md, Phases 1-3).

Upload a ZIP or point at a GitHub repository -> analysis and a recommended
target -> build, render, validate, scan, estimate -> plan against AWS using
read-only STS AssumeRole -> human approval gate.

All operations adhere to least-privilege read-only planning. Applying changes
remains gated until Phase 4.
"""

import asyncio
from datetime import datetime
import json
import logging
import os
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sse_starlette.sse import EventSourceResponse

from deploy.artifacts import get_artifact_store
from deploy.config import MAX_UPLOAD_BYTES, SOURCE_RETENTION_DAYS
from deploy.source_intake import IntakeError, download_github_archive, is_zip
from deploy.store import (
    DeployStatus,
    InvalidTransition,
    create_deployment,
    delete_artifact_record,
    get_deployment,
    list_deployments,
    list_events,
    new_deployment_id,
    record_artifact,
    transition,
    update_fields,
)
from models.deployment import (
    ApprovalRequest,
    BuildHistoryItem,
    CreatePullRequestRequest,
    DeployRequest,
    DeploymentAccepted,
    DeploymentDetail,
    DeploymentEventResponse,
    DeploymentSummary,
    GitHubSourceRequest,
    MergePullRequestRequest,
    PlanRequest,
    PrepareRequest,
    RejectionRequest,
    RenderedTerraformResponse,
    RollbackRequest,
    check_environment,
    check_region,
)
from models.orm import AwsDeployTarget
from services.auth import require_api_key
from services.database import SessionLocal
from services.identity import current_tenant, current_user, require_authenticated_user
from services.rate_limiter import limiter
from services.redis_client import redis_service
from tools.credential_scrubber import CredentialScrubber

logger = logging.getLogger("terraagent.routers.deployments")

router = APIRouter(prefix="/deployments", tags=["deployments"], dependencies=[Depends(require_api_key)])

# Inline (no-broker) runs: keep a reference so the event loop doesn't drop them.
_inline_tasks: Set["asyncio.Task[None]"] = set()

_PREPARABLE = frozenset({DeployStatus.ANALYZED, DeployStatus.VERIFIED, DeployStatus.FAILED})
_PLANNABLE = frozenset({
    DeployStatus.VERIFIED,
    DeployStatus.APPROVED,
    DeployStatus.REJECTED,
    DeployStatus.EXPIRED,
    DeployStatus.FAILED,
})


def _dispatch(stage: str, deployment_id: str) -> None:
    from deploy import pipeline, tasks

    if stage == "analyze":
        task = tasks.analyze_task
        run = pipeline.run_analysis
    elif stage == "build":
        task = tasks.build_and_verify_task
        run = pipeline.run_build_and_verify
    elif stage == "apply":
        task = tasks.apply_task
        run = lambda dep_id: asyncio.to_thread(tasks.apply_task, dep_id)
    elif stage == "destroy_plan":
        task = tasks.plan_destroy_task
        run = lambda dep_id: asyncio.to_thread(tasks.plan_destroy_task, dep_id)
    else:
        task = tasks.plan_task
        run = pipeline.run_plan

    if redis_service._redis_available:
        try:
            task.apply_async(args=[deployment_id], retry=False)
            return
        except Exception as e:
            logger.warning(f"[{deployment_id}] Celery dispatch failed, running inline: {e}")
    t = asyncio.create_task(run(deployment_id))
    _inline_tasks.add(t)
    t.add_done_callback(_inline_tasks.discard)


def _accept_source(
    data: bytes,
    source_kind: str,
    source_name: str,
    region: str,
    environment: str,
    requested_by: str = None,
    owner: str = None,
    tenant_id: str = None,
) -> str:
    deployment_id = new_deployment_id()
    stored = get_artifact_store().put_bytes(data)
    create_deployment(
        deployment_id,
        source_kind,
        source_name,
        region,
        environment,
        requested_by=requested_by,
        owner=owner,
        tenant_id=tenant_id,
    )
    update_fields(deployment_id, source_artifact_id=stored["artifact_id"], source_sha256=stored["sha256"])
    record_artifact(deployment_id, "source", stored, sensitive=True, retention_days=SOURCE_RETENTION_DAYS)
    _dispatch("analyze", deployment_id)
    return deployment_id


def _can_prepare(dep: Dict[str, Any]) -> bool:
    decision = dep.get("decision") or {}
    return DeployStatus(dep["status"]) in _PREPARABLE and bool(dep.get("profile")) and bool(decision.get("eligible"))


def _can_plan(dep: Dict[str, Any]) -> bool:
    return DeployStatus(dep["status"]) in _PLANNABLE and bool(dep.get("rendered"))


def _can_approve(dep: Dict[str, Any]) -> bool:
    return DeployStatus(dep["status"]) == DeployStatus.AWAITING_APPROVAL and bool(dep.get("plan_bundle_sha256"))


def _can_deploy(dep: Dict[str, Any]) -> bool:
    return DeployStatus(dep["status"]) == DeployStatus.APPROVED and bool(dep.get("approved_by"))


def _can_open_pr(dep: Dict[str, Any]) -> bool:
    return DeployStatus(dep["status"]) == DeployStatus.APPROVED and bool(dep.get("rendered"))


def _can_merge_pr(dep: Dict[str, Any]) -> bool:
    return DeployStatus(dep["status"]) == DeployStatus.PR_OPEN and bool(dep.get("pr"))


def _can_rollback(dep: Dict[str, Any]) -> bool:
    from deploy.rollback import _can_rollback as can_rb
    return can_rb(dep)


def _can_destroy(dep: Dict[str, Any]) -> bool:
    return DeployStatus(dep["status"]) in {
        DeployStatus.DEPLOYED,
        DeployStatus.FAILED_PARTIAL,
        DeployStatus.NEEDS_RECONCILIATION,
    } and bool(dep.get("target_id"))




@router.post("/upload", response_model=DeploymentAccepted, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("20/hour")
async def upload_source(
    request: Request,
    file: UploadFile = File(..., description="A .zip of the project"),
    region: str = Form("us-east-1"),
    environment: str = Form("production"),
) -> DeploymentAccepted:
    try:
        region, environment = check_region(region), check_environment(environment)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"The upload is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    if not is_zip(data):
        raise HTTPException(status_code=422, detail="The upload is not a ZIP archive")
    name = (file.filename or "upload.zip").replace("\\", "/").rsplit("/", 1)[-1][:200]
    user = current_user(request)
    tenant = current_tenant(request)
    deployment_id = _accept_source(data, "zip", name, region, environment, requested_by=user, owner=user, tenant_id=tenant)
    return DeploymentAccepted(deployment_id=deployment_id, status=DeployStatus.SOURCE_RECEIVED.value,
                              message="Source received; analysis started")


@router.post("/github", response_model=DeploymentAccepted, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("20/hour")
async def github_source(request: Request, body: GitHubSourceRequest) -> DeploymentAccepted:
    token = body.github_token.get_secret_value() if body.github_token else None
    try:
        data = await download_github_archive(body.repo, body.ref, token)
    except IntakeError as e:
        raise HTTPException(status_code=422, detail=CredentialScrubber.scrub_text(str(e)))
    except Exception as e:
        logger.warning(f"GitHub download failed for {body.repo}: {CredentialScrubber.scrub_text(str(e))}")
        raise HTTPException(status_code=502, detail="Could not download the repository from GitHub; try again")
    if not is_zip(data):
        raise HTTPException(status_code=502, detail="GitHub did not return a ZIP archive")
    name = f"{body.repo}@{body.ref}" if body.ref else body.repo
    user = current_user(request)
    tenant = current_tenant(request)
    deployment_id = _accept_source(data, "github", name, body.region, body.environment, requested_by=user, owner=user, tenant_id=tenant)
    return DeploymentAccepted(deployment_id=deployment_id, status=DeployStatus.SOURCE_RECEIVED.value,
                              message="Repository downloaded; analysis started")


@router.get("", response_model=List[DeploymentSummary])
async def get_deployments(limit: int = 50, request: Request = None) -> List[DeploymentSummary]:
    tenant = current_tenant(request)
    return [DeploymentSummary(**d) for d in list_deployments(max(1, min(limit, 200)), tenant_id=tenant)]


@router.post("/sandbox-test", status_code=status.HTTP_200_OK)
@limiter.limit("5/hour")
async def trigger_sandbox_test(request: Request = None, target_id: Optional[str] = None) -> Dict[str, Any]:
    """Triggers the scheduled end-to-end sandbox test suite immediately."""
    from deploy.sandbox_runner import run_sandbox_suite

    res = await run_sandbox_suite(target_id=target_id)
    return res


@router.get("/{deployment_id}", response_model=DeploymentDetail)
async def get_deployment_detail(deployment_id: str, request: Request = None) -> DeploymentDetail:

    tenant = current_tenant(request)
    dep = get_deployment(deployment_id, tenant_id=tenant)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return DeploymentDetail(
        **{k: v for k, v in dep.items() if k != "rendered"},
        rendered_files=sorted((dep.get("rendered") or {}).keys()),
        events=[DeploymentEventResponse(**e) for e in list_events(deployment_id, tenant_id=tenant)],
        can_prepare=_can_prepare(dep),
        can_plan=_can_plan(dep),
        can_approve=_can_approve(dep),
        can_deploy=_can_deploy(dep),
        can_open_pr=_can_open_pr(dep),
        can_merge_pr=_can_merge_pr(dep),
        can_rollback=_can_rollback(dep),
        can_destroy=_can_destroy(dep),
    )


@router.post("/{deployment_id}/prepare", response_model=DeploymentAccepted, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("30/hour")
async def prepare_deployment(request: Request, deployment_id: str, body: PrepareRequest) -> DeploymentAccepted:
    """Build, render and verify for the chosen target (one of the eligible ones)."""
    tenant = current_tenant(request)
    dep = get_deployment(deployment_id, tenant_id=tenant)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if not _can_prepare(dep):
        raise HTTPException(status_code=409, detail=f"The deployment can't be built while it is {dep['status']}")
    if body.target not in (dep.get("decision") or {}).get("eligible", []):
        raise HTTPException(status_code=422, detail=f"'{body.target}' is not an eligible target for this project")
    user = current_user(request) or "api"
    try:
        transition(deployment_id, DeployStatus.BUILDING, actor=user, reason=f"build requested for {body.target}",
                   allowed_from=_PREPARABLE, target_type=body.target, settings=body.settings.model_dump(),
                   build=None, rendered=None, verification=None, verdict=None, error=None)
    except InvalidTransition:
        raise HTTPException(status_code=409, detail="The deployment changed state; reload and try again")
    _dispatch("build", deployment_id)
    return DeploymentAccepted(deployment_id=deployment_id, status=DeployStatus.BUILDING.value,
                              message="Build and verification started")


@router.post("/{deployment_id}/plan", response_model=DeploymentAccepted, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("30/hour")
async def plan_deployment(request: Request, deployment_id: str, body: PlanRequest) -> DeploymentAccepted:
    """Run terraform plan with the target's read-only Plan role and pause at AWAITING_APPROVAL."""
    tenant = current_tenant(request)
    dep = get_deployment(deployment_id, tenant_id=tenant)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if not _can_plan(dep):
        raise HTTPException(status_code=409, detail=f"The deployment cannot be planned while it is {dep['status']}")

    # Verify target exists and matches tenant
    session = SessionLocal()
    try:
        target = session.get(AwsDeployTarget, body.target_id)
        if not target or (tenant and target.tenant_id and target.tenant_id != tenant):
            raise HTTPException(status_code=404, detail=f"Deploy target '{body.target_id}' not found")
    finally:
        session.close()

    user = current_user(request) or "api"
    try:
        transition(
            deployment_id,
            DeployStatus.PLANNING,
            actor=user,
            reason=f"Plan requested against target '{target.name}'",
            allowed_from=_PLANNABLE,
            target_id=body.target_id,
            plan=None,
            plan_summary=None,
            plan_bundle_sha256=None,
            plan_artifact_id=None,
            plan_policy=None,
            is_destructive=False,
            approved_by=None,
            approved_at=None,
            approval_reason=None,
            rejection_reason=None,
            error=None,
        )
    except InvalidTransition:
        raise HTTPException(status_code=409, detail="The deployment changed state; reload and try again")

    _dispatch("plan", deployment_id)
    return DeploymentAccepted(
        deployment_id=deployment_id,
        status=DeployStatus.PLANNING.value,
        message="Terraform planning started against target AWS account",
    )


@router.post("/{deployment_id}/approve", response_model=DeploymentAccepted)
@limiter.limit("20/hour")
async def approve_deployment(request: Request, deployment_id: str, body: ApprovalRequest) -> DeploymentAccepted:
    """Approves a plan bundle for deployment. Binds the approver's identity and hash."""
    tenant = current_tenant(request)
    dep = get_deployment(deployment_id, tenant_id=tenant)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if dep["status"] != DeployStatus.AWAITING_APPROVAL.value:
        raise HTTPException(status_code=409, detail=f"Deployment is {dep['status']}, not AWAITING_APPROVAL")

    if not body.confirm:
        raise HTTPException(status_code=422, detail="Explicit confirmation (confirm: true) is required to approve.")

    # 1. Identity requirement
    user = require_authenticated_user(request)

    # 2. Hash binding check
    expected_hash = dep.get("plan_bundle_sha256")
    if not expected_hash or body.plan_bundle_sha256.lower() != expected_hash.lower():
        raise HTTPException(
            status_code=400,
            detail="Plan bundle hash mismatch: the plan has changed since you reviewed it. Please reload and review the new plan.",
        )

    # 3. Expiration check (24 hours)
    # Fails closed: an unreadable timestamp counts as expired.
    try:
        planned_at = datetime.fromisoformat(dep.get("updated_at") or "")
        expired = (datetime.utcnow() - planned_at).total_seconds() > 86400
    except ValueError:
        expired = True
    if expired:
        raise HTTPException(status_code=400, detail="The approval window for this plan has expired (24 hours). Please re-plan.")

    # 4. Destructive change acknowledgement check
    if (dep.get("is_destructive") or dep.get("plan_kind") == "destroy") and not body.acknowledge_destructive:
        raise HTTPException(
            status_code=400,
            detail="This plan contains destructive changes (replace or destroy). You must explicitly acknowledge destructive changes.",
        )

    now = datetime.utcnow().isoformat()
    try:
        transition(
            deployment_id,
            DeployStatus.APPROVED,
            actor=user,
            reason=body.reason or "Plan approved by user",
            allowed_from=frozenset({DeployStatus.AWAITING_APPROVAL}),
            approved_by=user,
            approved_at=now,
            approval_reason=body.reason,
        )
    except InvalidTransition:
        raise HTTPException(status_code=409, detail="The deployment changed state; reload and try again")

    await redis_service.publish_log(
        deployment_id,
        f"[DEPLOY] Plan APPROVED by {user}. Hash: {expected_hash[:16]}... Ready for deployment.",
        agent_name="deploy",
    )

    return DeploymentAccepted(
        deployment_id=deployment_id,
        status=DeployStatus.APPROVED.value,
        message=f"Plan approved by {user}. Ready for execution.",
    )


@router.post("/{deployment_id}/deploy", response_model=DeploymentAccepted)
@limiter.limit("20/hour")
async def deploy_deployment(request: Request, deployment_id: str, body: DeployRequest) -> DeploymentAccepted:
    """Executes an approved deployment against AWS using the apply role (Phase 4A)."""
    tenant = current_tenant(request)
    dep = get_deployment(deployment_id, tenant_id=tenant)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if dep["status"] != DeployStatus.APPROVED.value:
        raise HTTPException(status_code=409, detail=f"Deployment must be APPROVED to deploy (currently {dep['status']})")

    if not body.confirm:
        raise HTTPException(status_code=422, detail="Explicit confirmation (confirm: true) is required to deploy.")

    user = require_authenticated_user(request)

    # apply_approved re-checks this (it is the real gate), but its refusal happens in the
    # background where the caller never sees it; refuse here so the UI gets the reason.
    if os.environ.get("TERRAAGENT_DEPLOY_ENABLED", "").lower() != "true":
        raise HTTPException(
            status_code=409,
            detail="Deploying to AWS is switched off on this TerraAgent server. Set TERRAAGENT_DEPLOY_ENABLED=true "
                   "in its environment and restart the API. Nothing was sent to AWS.",
        )

    _dispatch("apply", deployment_id)
    return DeploymentAccepted(
        deployment_id=deployment_id,
        status=DeployStatus.APPLYING.value,
        message=f"Deployment execution initiated by {user}.",
    )


@router.post("/{deployment_id}/reject", response_model=DeploymentAccepted)
@limiter.limit("20/hour")
async def reject_deployment(request: Request, deployment_id: str, body: RejectionRequest) -> DeploymentAccepted:
    """Rejects a plan, deleting sensitive plan artifacts."""
    tenant = current_tenant(request)
    dep = get_deployment(deployment_id, tenant_id=tenant)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if dep["status"] != DeployStatus.AWAITING_APPROVAL.value:
        raise HTTPException(status_code=409, detail=f"Deployment is {dep['status']}, not AWAITING_APPROVAL")

    user = current_user(request) or "api"
    plan_art_id = dep.get("plan_artifact_id")

    try:
        transition(
            deployment_id,
            DeployStatus.REJECTED,
            actor=user,
            reason=body.reason or "Plan rejected by user",
            allowed_from=frozenset({DeployStatus.AWAITING_APPROVAL}),
            rejection_reason=body.reason,
        )
    except InvalidTransition:
        raise HTTPException(status_code=409, detail="The deployment changed state; reload and try again")

    if plan_art_id:
        store = get_artifact_store()
        store.delete(plan_art_id)
        delete_artifact_record(plan_art_id)

    await redis_service.publish_log(
        deployment_id,
        f"[DEPLOY] Plan REJECTED by {user}. Reason: {body.reason or 'None provided'}",
        agent_name="deploy",
    )

    return DeploymentAccepted(
        deployment_id=deployment_id,
        status=DeployStatus.REJECTED.value,
        message="Plan rejected.",
    )


@router.post("/{deployment_id}/pull-request", response_model=Dict[str, Any])
@limiter.limit("20/hour")
async def create_pull_request_endpoint(
    request: Request,
    deployment_id: str,
    body: CreatePullRequestRequest,
) -> Dict[str, Any]:
    """Generates a GitHub Pull Request with the deployment's Terraform code and CI workflows."""
    tenant = current_tenant(request)
    dep = get_deployment(deployment_id, tenant_id=tenant)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if not _can_open_pr(dep):
        raise HTTPException(status_code=409, detail=f"Deployment must be APPROVED to open a PR (status: {dep['status']})")

    from deploy.executor import GitOpsPrExecutor
    from services.github_client import GitHubPullRequestError

    executor = GitOpsPrExecutor(
        github_token=body.github_token.get_secret_value(),
        repo=body.repo,
        base_branch=body.base_branch,
        target_dir=body.target_dir,
        add_workflows=body.add_workflows,
    )

    try:
        result = await executor.execute(deployment_id)
        return result
    except GitHubPullRequestError as e:
        logger.warning(f"[{deployment_id}] GitHub PR creation failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.exception(f"[{deployment_id}] PR creation crashed")
        raise HTTPException(status_code=500, detail=f"Failed to create Pull Request: {e}")


@router.get("/{deployment_id}/pull-request", response_model=Dict[str, Any])
async def get_pull_request_status_endpoint(deployment_id: str, request: Request = None) -> Dict[str, Any]:
    tenant = current_tenant(request)
    dep = get_deployment(deployment_id, tenant_id=tenant)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    pr_data = dep.get("pr")
    if not pr_data:
        raise HTTPException(status_code=404, detail="No Pull Request associated with this deployment")
    return {"deployment_id": deployment_id, "pr": pr_data, "status": dep["status"]}


@router.post("/{deployment_id}/pull-request/merge", response_model=Dict[str, Any])
@limiter.limit("20/hour")
async def merge_pull_request_endpoint(
    request: Request,
    deployment_id: str,
    body: MergePullRequestRequest,
) -> Dict[str, Any]:
    """Merges the open Pull Request on GitHub and advances deployment status to MERGED."""
    tenant = current_tenant(request)
    dep = get_deployment(deployment_id, tenant_id=tenant)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if dep["status"] != DeployStatus.PR_OPEN.value:
        raise HTTPException(status_code=409, detail=f"Deployment is {dep['status']}, not PR_OPEN")

    pr_data = dep.get("pr") or {}
    pr_number = pr_data.get("number")
    repo = pr_data.get("repo")
    if not pr_number or not repo:
        raise HTTPException(status_code=422, detail="Missing PR number or repository in deployment record")

    from services.github_client import GitHubPullRequestError, merge_pr

    try:
        merge_res = await merge_pr(
            github_token=body.github_token.get_secret_value(),
            repo=repo,
            pr_number=pr_number,
            merge_method=body.merge_method,
            commit_title=body.commit_title,
            commit_message=body.commit_message,
        )
    except GitHubPullRequestError as e:
        logger.warning(f"[{deployment_id}] Merge failed on GitHub: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.exception(f"[{deployment_id}] Merge failed unexpectedly")
        raise HTTPException(status_code=500, detail=f"Merge failed: {e}")

    user = current_user(request) or "api"
    try:
        transition(
            deployment_id,
            DeployStatus.MERGED,
            actor=user,
            reason=f"Merged PR #{pr_number} into base branch",
            allowed_from=frozenset({DeployStatus.PR_OPEN}),
            completed_at=datetime.utcnow().isoformat(),
        )
    except InvalidTransition:
        raise HTTPException(status_code=409, detail="Deployment state transition failed")

    await redis_service.publish_log(
        deployment_id,
        f"[GITOPS] PR #{pr_number} successfully merged by {user}. Deployment marked as MERGED.",
        agent_name="deploy",
    )

    return {
        "deployment_id": deployment_id,
        "status": DeployStatus.MERGED.value,
        "merged": True,
        "sha": merge_res.get("sha"),
        "message": merge_res.get("message"),
    }


@router.post("/{deployment_id}/rollback", response_model=DeploymentAccepted, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("20/hour")
async def rollback_deployment(
    request: Request,
    deployment_id: str,
    body: RollbackRequest = RollbackRequest(),
) -> DeploymentAccepted:
    """Prepares and plans a zero-downtime rollback to a prior build/release."""
    tenant = current_tenant(request)
    dep = get_deployment(deployment_id, tenant_id=tenant)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if not _can_rollback(dep):
        raise HTTPException(
            status_code=409,
            detail=f"Deployment must be in a deployed or partial state to roll back (currently {dep['status']})",
        )

    from deploy.rollback import prepare_rollback

    user = current_user(request) or "api"
    try:
        res = await prepare_rollback(
            deployment_id,
            target_artifact_id=body.target_artifact_id,
            target_release_id=body.target_release_id,
            actor=user,
            reason=body.reason,
        )
    except Exception as e:
        logger.exception(f"[{deployment_id}] Rollback preparation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    _dispatch("plan", deployment_id)
    return DeploymentAccepted(
        deployment_id=deployment_id,
        status=DeployStatus.PLANNING.value,
        message=res["message"],
    )


@router.post("/{deployment_id}/destroy/plan", response_model=DeploymentAccepted, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("20/hour")
async def plan_destroy_deployment(request: Request, deployment_id: str) -> DeploymentAccepted:
    """Phase 5.2: Initiates a destroy plan against AWS resources for this deployment."""
    tenant = current_tenant(request)
    dep = get_deployment(deployment_id, tenant_id=tenant)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if not _can_destroy(dep):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot plan destroy from status '{dep['status']}'; deployment must be in DEPLOYED, FAILED_PARTIAL, or NEEDS_RECONCILIATION.",
        )

    user = current_user(request) or "api"
    _dispatch("destroy_plan", deployment_id)
    return DeploymentAccepted(
        deployment_id=deployment_id,
        status=DeployStatus.DESTROY_PLANNING.value,
        message="Destroy plan generation initiated",
    )


@router.get("/{deployment_id}/artifacts", response_model=List[BuildHistoryItem])
async def get_deployment_artifacts(deployment_id: str, request: Request = None) -> List[BuildHistoryItem]:
    """Returns recent build and source artifacts available for this deployment."""
    tenant = current_tenant(request)
    dep = get_deployment(deployment_id, tenant_id=tenant)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")

    from deploy.store import list_deployment_build_artifacts

    items = list_deployment_build_artifacts(deployment_id, limit=10)
    return [BuildHistoryItem(**item) for item in items]


@router.get("/{deployment_id}/terraform", response_model=RenderedTerraformResponse)
async def get_rendered_terraform(deployment_id: str, request: Request = None) -> RenderedTerraformResponse:
    tenant = current_tenant(request)
    dep = get_deployment(deployment_id, tenant_id=tenant)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return RenderedTerraformResponse(deployment_id=deployment_id, files=dep.get("rendered") or {})




@router.get("/{deployment_id}/logs/history")
async def get_deployment_logs(deployment_id: str, request: Request = None) -> Dict[str, Any]:
    tenant = current_tenant(request)
    dep = get_deployment(deployment_id, tenant_id=tenant)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    parsed = []
    for item in await redis_service.get_log_history(deployment_id):
        try:
            parsed.append(json.loads(item))
        except Exception:
            parsed.append({"job_id": deployment_id, "message": str(item), "agent": "deploy", "seq": None})
    return {"deployment_id": deployment_id, "logs": parsed, "count": len(parsed)}


@router.get("/{deployment_id}/logs")
async def stream_deployment_logs(deployment_id: str, request: Request = None) -> EventSourceResponse:
    tenant = current_tenant(request)
    dep = get_deployment(deployment_id, tenant_id=tenant)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    async def events():
        async for message in redis_service.subscribe_logs(deployment_id):
            yield {"event": "message", "data": message}

    return EventSourceResponse(events())
