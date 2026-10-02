"""Tests for Per-Tenant Ownership and Isolation (Phase 6, Task 3).

Ensures all deployment and deploy-target read/write operations strictly enforce
tenant isolation and return 404 Not Found (never 403) on cross-tenant requests.
"""

import asyncio
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from deploy.store import create_deployment, get_deployment, list_deployments, list_events
from models.deployment import PlanRequest, PrepareStaticSite, RollbackRequest
from models.orm import AwsDeployTarget, Deployment
from models.target import AwsDeployTargetCreate
from routers.aws_targets import (
    create_deploy_target,
    delete_deploy_target,
    get_deploy_target,
    list_deploy_targets,
)
from routers.deployments import (
    get_deployment_artifacts,
    get_deployment_detail,
    get_deployment_logs,
    get_deployments,
    get_rendered_terraform,
    plan_deployment,
    plan_destroy_deployment,
    prepare_deployment,
    rollback_deployment,
)
from services.database import SessionLocal, init_db


@pytest.fixture(autouse=True)
def _trusted_sso_proxy(monkeypatch):
    # These tests model a deployment behind an authenticating SSO proxy, the
    # only setup in which identity/tenant headers are trusted (services/identity.py).
    monkeypatch.setenv("TERRAAGENT_TRUSTED_PROXY_AUTH", "true")


def _make_request(tenant: str = None, email: str = None) -> Request:
    headers = []
    if tenant:
        headers.append((b"x-tenant-id", tenant.encode("utf-8")))
    if email:
        headers.append((b"x-forwarded-email", email.encode("utf-8")))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "query_string": b""})


@pytest.fixture(autouse=True)
def cleanup_db():
    init_db()
    session = SessionLocal()
    try:
        session.query(Deployment).delete()
        session.query(AwsDeployTarget).delete()
        session.commit()
    except Exception:
        pass
    finally:
        session.close()
    yield
    session = SessionLocal()
    try:
        session.query(Deployment).delete()
        session.query(AwsDeployTarget).delete()
        session.commit()
    except Exception:
        pass
    finally:
        session.close()


def test_store_tenant_isolation():
    # Create deployment for tenant-alpha
    dep_a = create_deployment(
        "dep-tenant-a-1",
        "zip",
        "app.zip",
        "us-east-1",
        "production",
        owner="alice@alpha.com",
        tenant_id="tenant-alpha",
    )
    # Create deployment for tenant-beta
    dep_b = create_deployment(
        "dep-tenant-b-1",
        "zip",
        "app.zip",
        "us-east-1",
        "production",
        owner="bob@beta.com",
        tenant_id="tenant-beta",
    )

    # 1. get_deployment matching tenant
    assert get_deployment("dep-tenant-a-1", tenant_id="tenant-alpha") is not None
    assert get_deployment("dep-tenant-b-1", tenant_id="tenant-beta") is not None

    # 2. get_deployment cross-tenant returns None
    assert get_deployment("dep-tenant-a-1", tenant_id="tenant-beta") is None
    assert get_deployment("dep-tenant-b-1", tenant_id="tenant-alpha") is None

    # 3. list_deployments filters by tenant
    alpha_deps = list_deployments(tenant_id="tenant-alpha")
    assert len(alpha_deps) == 1
    assert alpha_deps[0]["id"] == "dep-tenant-a-1"

    beta_deps = list_deployments(tenant_id="tenant-beta")
    assert len(beta_deps) == 1
    assert beta_deps[0]["id"] == "dep-tenant-b-1"

    # 4. list_events cross-tenant returns empty
    assert list_events("dep-tenant-a-1", tenant_id="tenant-beta") == []
    assert len(list_events("dep-tenant-a-1", tenant_id="tenant-alpha")) >= 1


def test_aws_target_tenant_isolation():
    req_alpha = _make_request(tenant="tenant-alpha", email="alice@alpha.com")
    req_beta = _make_request(tenant="tenant-beta", email="bob@beta.com")

    payload = AwsDeployTargetCreate(
        name="Alpha Prod",
        account_id="123456789012",
        region="us-east-1",
        plan_role_arn="arn:aws:iam::123456789012:role/TerraAgentDeployPlan",
        apply_role_arn="arn:aws:iam::123456789012:role/TerraAgentDeployApply",
        state_bucket="alpha-state-bucket",
    )

    # 1. Create target under tenant-alpha
    target = asyncio.run(create_deploy_target(payload, request=req_alpha))
    assert target.tenant_id == "tenant-alpha"
    assert target.owner == "alice@alpha.com"

    # 2. Tenant-alpha can access target
    fetched = asyncio.run(get_deploy_target(target.id, request=req_alpha))
    assert fetched.id == target.id

    # 3. Tenant-beta gets 404 when accessing or deleting tenant-alpha's target
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_deploy_target(target.id, request=req_beta))
    assert exc_info.value.status_code == 404

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(delete_deploy_target(target.id, request=req_beta))
    assert exc_info.value.status_code == 404

    # 4. list_deploy_targets isolates targets
    alpha_list = asyncio.run(list_deploy_targets(request=req_alpha))
    assert len(alpha_list) == 1
    assert alpha_list[0].id == target.id

    beta_list = asyncio.run(list_deploy_targets(request=req_beta))
    assert len(beta_list) == 0


def test_deployments_router_cross_tenant_404():
    req_alpha = _make_request(tenant="tenant-alpha", email="alice@alpha.com")
    req_beta = _make_request(tenant="tenant-beta", email="bob@beta.com")

    dep = create_deployment(
        "dep-alpha-xyz",
        "zip",
        "app.zip",
        "us-east-1",
        "production",
        owner="alice@alpha.com",
        tenant_id="tenant-alpha",
    )

    # Tenant alpha can get detail
    detail = asyncio.run(get_deployment_detail(dep["id"], request=req_alpha))
    assert detail.id == dep["id"]

    # Tenant beta gets 404 for all operations
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_deployment_detail(dep["id"], request=req_beta))
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        asyncio.run(prepare_deployment(req_beta, dep["id"], PrepareStaticSite(target="static_site")))
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        asyncio.run(plan_deployment(req_beta, dep["id"], PlanRequest(target_id="target-123")))
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        asyncio.run(rollback_deployment(req_beta, dep["id"], RollbackRequest()))
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        asyncio.run(plan_destroy_deployment(req_beta, dep["id"]))
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_deployment_artifacts(dep["id"], request=req_beta))
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_rendered_terraform(dep["id"], request=req_beta))
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_deployment_logs(dep["id"], request=req_beta))
    assert exc.value.status_code == 404

    # list deployments returns only own deployments
    deps_beta = asyncio.run(get_deployments(request=req_beta))
    assert len(deps_beta) == 0

    deps_alpha = asyncio.run(get_deployments(request=req_alpha))
    assert len(deps_alpha) == 1
    assert deps_alpha[0].id == dep["id"]
