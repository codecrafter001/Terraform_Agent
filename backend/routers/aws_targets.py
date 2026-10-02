"""Router for AWS Deploy Targets (Phase 3).

Allows customers to register their AWS account roles and state bucket,
generates per-target ExternalIds for confused-deputy protection, and provides
a verification endpoint to test AssumeRole and bucket connectivity.
"""

from datetime import datetime
import logging
import secrets
from typing import List, Optional
import uuid

import boto3
from fastapi import APIRouter, Depends, HTTPException, Request, status

from models.orm import AwsDeployTarget
from models.target import (
    AwsDeployTargetCreate,
    AwsDeployTargetResponse,
    AwsDeployTargetVerifyResult,
)
from services.auth import require_api_key
from services.database import SessionLocal
from services.identity import current_tenant, current_user
from tools.credential_scrubber import CredentialScrubber
from tools.sts_helper import assume_role

logger = logging.getLogger("terraagent.routers.aws_targets")

router = APIRouter(prefix="/aws-targets", tags=["deploy-targets"], dependencies=[Depends(require_api_key)])


def _now() -> str:
    return datetime.utcnow().isoformat()


def _to_response(rec: AwsDeployTarget) -> AwsDeployTargetResponse:
    return AwsDeployTargetResponse(
        id=rec.id,
        name=rec.name,
        account_id=rec.account_id,
        region=rec.region,
        plan_role_arn=rec.plan_role_arn,
        apply_role_arn=rec.apply_role_arn,
        permissions_boundary_arn=rec.permissions_boundary_arn,
        state_bucket=rec.state_bucket,
        external_id=rec.external_id,
        owner=getattr(rec, "owner", None),
        tenant_id=getattr(rec, "tenant_id", None),
        verified_at=rec.verified_at,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
    )


@router.post("", response_model=AwsDeployTargetResponse, status_code=status.HTTP_201_CREATED)
async def create_deploy_target(body: AwsDeployTargetCreate, request: Request = None) -> AwsDeployTargetResponse:
    target_id = f"target-{uuid.uuid4().hex[:12]}"
    external_id = secrets.token_urlsafe(24)
    now = _now()
    user = current_user(request)
    tenant = current_tenant(request)

    session = SessionLocal()
    try:
        rec = AwsDeployTarget(
            id=target_id,
            name=body.name.strip(),
            account_id=body.account_id.strip(),
            region=body.region.strip(),
            plan_role_arn=body.plan_role_arn.strip(),
            apply_role_arn=body.apply_role_arn.strip(),
            permissions_boundary_arn=body.permissions_boundary_arn.strip() if body.permissions_boundary_arn else None,
            state_bucket=body.state_bucket.strip(),
            external_id=external_id,
            owner=user,
            tenant_id=tenant,
            verified_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(rec)
        session.commit()
        session.refresh(rec)
        return _to_response(rec)
    finally:
        session.close()


@router.get("", response_model=List[AwsDeployTargetResponse])
async def list_deploy_targets(request: Request = None) -> List[AwsDeployTargetResponse]:
    tenant = current_tenant(request)
    session = SessionLocal()
    try:
        query = session.query(AwsDeployTarget)
        if tenant:
            query = query.filter(AwsDeployTarget.tenant_id == tenant)
        rows = query.order_by(AwsDeployTarget.created_at.desc()).all()
        return [_to_response(r) for r in rows]
    finally:
        session.close()


@router.get("/{target_id}", response_model=AwsDeployTargetResponse)
async def get_deploy_target(target_id: str, request: Request = None) -> AwsDeployTargetResponse:
    tenant = current_tenant(request)
    session = SessionLocal()
    try:
        rec = session.get(AwsDeployTarget, target_id)
        if not rec or (tenant and rec.tenant_id and rec.tenant_id != tenant):
            raise HTTPException(status_code=404, detail="Deploy target not found")
        return _to_response(rec)
    finally:
        session.close()


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deploy_target(target_id: str, request: Request = None) -> None:
    tenant = current_tenant(request)
    session = SessionLocal()
    try:
        rec = session.get(AwsDeployTarget, target_id)
        if not rec or (tenant and rec.tenant_id and rec.tenant_id != tenant):
            raise HTTPException(status_code=404, detail="Deploy target not found")
        session.delete(rec)
        session.commit()
    finally:
        session.close()


@router.post("/{target_id}/verify", response_model=AwsDeployTargetVerifyResult)
async def verify_deploy_target(target_id: str, request: Request = None) -> AwsDeployTargetVerifyResult:
    """Verifies that the target's Plan role, Apply role, and State bucket are reachable."""
    tenant = current_tenant(request)
    session = SessionLocal()
    try:
        rec = session.get(AwsDeployTarget, target_id)
        if not rec or (tenant and rec.tenant_id and rec.tenant_id != tenant):
            raise HTTPException(status_code=404, detail="Deploy target not found")
        target_dict = {
            "id": rec.id,
            "account_id": rec.account_id,
            "region": rec.region,
            "plan_role_arn": rec.plan_role_arn,
            "apply_role_arn": rec.apply_role_arn,
            "state_bucket": rec.state_bucket,
            "external_id": rec.external_id,
        }
    finally:
        session.close()

    plan_ok = False
    apply_ok = False
    bucket_ok = False
    caller_id: Optional[dict] = None
    messages: List[str] = []

    # 1. Test Plan Role
    try:
        plan_ak, plan_sk, plan_st = assume_role(
            role_arn=target_dict["plan_role_arn"],
            region=target_dict["region"],
            session_name=f"verify-plan-{target_id}"[:64],
            external_id=target_dict["external_id"],
            duration_seconds=900,
        )
        plan_sess = boto3.Session(
            aws_access_key_id=plan_ak,
            aws_secret_access_key=plan_sk,
            aws_session_token=plan_st,
            region_name=target_dict["region"],
        )
        sts_client = plan_sess.client("sts")
        id_info = sts_client.get_caller_identity()
        caller_id = {
            "account": id_info.get("Account"),
            "arn": id_info.get("Arn"),
            "user_id": id_info.get("UserId"),
        }
        if caller_id["account"] != target_dict["account_id"]:
            messages.append(f"Account ID mismatch: expected {target_dict['account_id']}, got {caller_id['account']}")
        else:
            plan_ok = True
    except Exception as e:
        messages.append(f"Plan role verification failed: {CredentialScrubber.scrub_text(str(e))}")

    # 2. Test State Bucket using plan role credentials if assumed, or direct check
    if plan_ok:
        try:
            s3_client = plan_sess.client("s3")
            s3_client.list_objects_v2(Bucket=target_dict["state_bucket"], MaxKeys=1)
            bucket_ok = True
        except Exception as e:
            messages.append(f"State bucket check failed: {CredentialScrubber.scrub_text(str(e))}")
    else:
        messages.append("State bucket check skipped due to Plan role failure")

    # 3. Test Apply Role
    try:
        assume_role(
            role_arn=target_dict["apply_role_arn"],
            region=target_dict["region"],
            session_name=f"verify-apply-{target_id}"[:64],
            external_id=target_dict["external_id"],
            duration_seconds=900,
        )
        apply_ok = True
    except Exception as e:
        messages.append(f"Apply role verification failed: {CredentialScrubber.scrub_text(str(e))}")

    overall_ok = plan_ok and bucket_ok and apply_ok

    if overall_ok:
        now = _now()
        db_sess = SessionLocal()
        try:
            db_rec = db_sess.get(AwsDeployTarget, target_id)
            if db_rec:
                db_rec.verified_at = now
                db_rec.updated_at = now
                db_sess.commit()
        finally:
            db_sess.close()

    summary_message = "All roles and state bucket verified successfully." if overall_ok else "; ".join(messages)

    return AwsDeployTargetVerifyResult(
        target_id=target_id,
        verified=overall_ok,
        plan_role_ok=plan_ok,
        apply_role_ok=apply_ok,
        bucket_ok=bucket_ok,
        caller_identity=caller_id,
        message=summary_message,
    )
