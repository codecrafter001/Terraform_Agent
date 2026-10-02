"""Approver identity must come only from a source the caller can't forge
(services/identity.py), and an expired plan can never be approved."""

import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import routers.deployments as api
from deploy import store
from deploy.store import DeployStatus
from models.deployment import ApprovalRequest
from services.database import init_db
from services.identity import current_tenant, current_user, require_authenticated_user
from services.rate_limiter import limiter


def _req(headers=None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw, "query_string": b""})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("TERRAAGENT_TRUSTED_PROXY_AUTH", raising=False)
    monkeypatch.delenv("TERRAAGENT_OPERATOR_EMAIL", raising=False)


def test_client_headers_are_ignored_without_a_trusted_proxy():
    spoof = _req({"X-Forwarded-Email": "ceo@corp.com", "X-Tenant": "other", "Authorization": "Bearer a@b.c",
                  "X-API-Key": "supersecretkey"})
    assert current_user(spoof) is None
    assert current_tenant(spoof) is None
    with pytest.raises(HTTPException) as e:
        require_authenticated_user(spoof)
    assert e.value.status_code == 401 and "TERRAAGENT_OPERATOR_EMAIL" in e.value.detail


def test_operator_email_is_the_identity_and_headers_cannot_override_it(monkeypatch):
    monkeypatch.setenv("TERRAAGENT_OPERATOR_EMAIL", "Ops@Acme.io")
    assert current_user(_req({"X-Forwarded-Email": "ceo@corp.com"})) == "ops@acme.io"
    assert current_tenant(_req()) is None  # single tenant: no filtering


def test_trusted_proxy_headers_are_used_only_when_enabled(monkeypatch):
    monkeypatch.setenv("TERRAAGENT_TRUSTED_PROXY_AUTH", "true")
    monkeypatch.setenv("TERRAAGENT_OPERATOR_EMAIL", "ops@acme.io")
    req = _req({"X-Forwarded-Email": "Dev@Acme.io"})
    assert current_user(req) == "dev@acme.io"
    assert current_tenant(req) == "acme.io"
    assert current_user(_req()) is None  # proxy mode: no header, no identity
    assert current_user(_req({"X-Forwarded-Email": "bad value; drop table"})) is None


def test_api_key_never_becomes_an_identity(monkeypatch):
    monkeypatch.setenv("TERRAAGENT_TRUSTED_PROXY_AUTH", "true")
    assert current_user(_req({"X-API-Key": "supersecretkey"})) is None


def _awaiting(plan_age: timedelta) -> str:
    init_db()
    dep_id = store.new_deployment_id()
    store.create_deployment(dep_id, "zip", "x.zip", "us-east-1", "production")
    for status in (DeployStatus.ANALYZING, DeployStatus.ANALYZED, DeployStatus.BUILDING, DeployStatus.VERIFYING,
                   DeployStatus.VERIFIED, DeployStatus.PLANNING):
        store.transition(dep_id, status)
    store.transition(dep_id, DeployStatus.AWAITING_APPROVAL, plan_bundle_sha256="a" * 64)
    from models.orm import Deployment
    from services.database import SessionLocal

    session = SessionLocal()
    try:
        rec = session.get(Deployment, dep_id)
        rec.updated_at = (datetime.utcnow() - plan_age).isoformat()
        session.commit()
    finally:
        session.close()
    return dep_id


def test_expired_plan_cannot_be_approved(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setenv("TERRAAGENT_OPERATOR_EMAIL", "ops@acme.io")
    dep_id = _awaiting(timedelta(hours=25))
    body = ApprovalRequest(plan_bundle_sha256="a" * 64, confirm=True)
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.approve_deployment(_req(), dep_id, body))
    assert e.value.status_code == 400 and "expired" in e.value.detail
    assert store.get_deployment(dep_id)["status"] == "AWAITING_APPROVAL"


def test_fresh_plan_is_approved_by_the_operator(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setenv("TERRAAGENT_OPERATOR_EMAIL", "ops@acme.io")
    dep_id = _awaiting(timedelta(minutes=5))
    body = ApprovalRequest(plan_bundle_sha256="a" * 64, confirm=True)
    asyncio.run(api.approve_deployment(_req({"X-Forwarded-Email": "ceo@corp.com"}), dep_id, body))
    dep = store.get_deployment(dep_id)
    assert dep["status"] == "APPROVED" and dep["approved_by"] == "ops@acme.io"


def test_approval_without_identity_is_refused(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    dep_id = _awaiting(timedelta(minutes=5))
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.approve_deployment(_req({"X-Forwarded-Email": "ceo@corp.com"}), dep_id,
                                           ApprovalRequest(plan_bundle_sha256="a" * 64, confirm=True)))
    assert e.value.status_code == 401
