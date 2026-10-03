"""Code updates of a deployed stack (deploy/code_update.py): new source reuses the
deployment, the pipeline chains analyze -> build -> verify -> plan by itself and
stops at AWAITING_APPROVAL, verification is reused when the .tf files are
unchanged, and a source-only plan is flagged code_only and not destructive.

The builder and renderer run for real; Terraform plan, STS and the scanners are
replaced (like test_deploy_flow), and the API routes are called directly."""

import asyncio
import io
import zipfile
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException, UploadFile
from starlette.requests import Request

import deploy.pipeline as pipeline
import routers.deployments as api
from deploy import code_update, store
from deploy.artifacts import LocalArtifactStore
from deploy.store import DeployStatus
from models.deployment import EcsSettings, PrepareEcs
from models.orm import AwsDeployTarget
from services.database import SessionLocal, init_db
from services.rate_limiter import limiter

SERVER_V1 = "const express = require('express');\nconst app = express();\napp.get('/', (q, r) => r.send('v1'));\napp.listen(process.env.PORT || 3000);\n"
SERVER_V2 = SERVER_V1.replace("'v1'", "'v2'")
PACKAGE = '{"name": "app", "scripts": {"start": "node server.js"}, "dependencies": {"express": "^4.19.0"}}'

CODE_ONLY_CHANGES = [
    {"address": "aws_s3_object.source", "action": "update", "type": "aws_s3_object"},
    {"address": "aws_codebuild_project.builder", "action": "update", "type": "aws_codebuild_project"},
    {"address": "aws_ecs_task_definition.app", "action": "replace", "type": "aws_ecs_task_definition"},
    {"address": "aws_ecs_service.app", "action": "update", "type": "aws_ecs_service"},
]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    init_db()
    monkeypatch.setattr(limiter, "enabled", False)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    for module in (api, pipeline):
        monkeypatch.setattr(module, "get_artifact_store", lambda: artifacts)
    dispatched = []
    monkeypatch.setattr(api, "_dispatch", lambda stage, dep_id: dispatched.append((stage, dep_id)))
    calls = {"verify": 0, "plan_changes": CODE_ONLY_CHANGES}

    async def fake_verify(deployment_id, files, binary="terraform"):
        calls["verify"] += 1
        return {"verdict": "PASS", "incomplete_reasons": [], "validation": {"passed": True, "checks": []},
                "security": {"findings": []}, "security_posture": {"score": 100}, "cost": {"total_monthly_cost": 40.0}}

    async def fake_plan_saved(workdir, target, deployment_id, aws_credentials, region):
        changes = calls["plan_changes"]
        counts = {"create": 0, "update": 0, "replace": 0, "destroy": 0, "no_op": 0}
        for c in changes:
            counts[c["action"]] += 1
        return {"passed": True, "checks": [], "plan_json": {}, "raw_plan_json": {"resource_changes": []},
                "plan_binary": b"plan", "counts": counts, "changes": changes,
                "is_destructive": bool(counts["replace"] or counts["destroy"])}

    monkeypatch.setattr(pipeline, "verify", fake_verify)
    monkeypatch.setattr(pipeline, "plan_session", lambda target, dep_id: {"AWS_ACCESS_KEY_ID": "x"})
    monkeypatch.setattr(pipeline.TerraformRunner, "plan_saved", staticmethod(fake_plan_saved))
    return {"dispatched": dispatched, "calls": calls}


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""})


def _zip(files) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _target() -> str:
    target_id = "target-" + store.new_deployment_id()[4:]
    now = datetime.now(timezone.utc).isoformat()
    session = SessionLocal()
    try:
        session.add(AwsDeployTarget(
            id=target_id, name="Test", account_id="123456789012", region="us-east-1",
            plan_role_arn="arn:aws:iam::123456789012:role/TerraAgentDeployPlan",
            apply_role_arn="arn:aws:iam::123456789012:role/TerraAgentDeployApply",
            state_bucket="state-bucket", external_id="x" * 20, created_at=now, updated_at=now,
        ))
        session.commit()
    finally:
        session.close()
    return target_id


def _deployed_app(server: str = SERVER_V1) -> str:
    """Uploads, analyses, builds and plans v1, then marks it DEPLOYED as apply would."""
    upload = UploadFile(file=io.BytesIO(_zip({"package.json": PACKAGE, "server.js": server})), filename="app.zip")
    dep_id = asyncio.run(api.upload_source(_request(), file=upload, region="us-east-1", environment="production")).deployment_id
    asyncio.run(pipeline.run_analysis(dep_id))
    asyncio.run(api.prepare_deployment(_request(), dep_id, PrepareEcs(target="ecs_service", settings=EcsSettings(container_port=3000))))
    asyncio.run(pipeline.run_build_and_verify(dep_id))
    target_id = _target()
    store.transition(dep_id, DeployStatus.PLANNING, target_id=target_id)
    asyncio.run(pipeline.run_plan(dep_id))
    store.transition(dep_id, DeployStatus.APPROVED, approved_by="ops@example.com",
                     approved_at=datetime.now(timezone.utc).isoformat())
    store.transition(dep_id, DeployStatus.APPLYING)
    now = datetime.now(timezone.utc).isoformat()
    store.transition(dep_id, DeployStatus.DEPLOYED, applied_at=now, outputs={"url": "http://example"})
    return dep_id


def _push_update(dep_id: str, server: str = SERVER_V2) -> None:
    upload = UploadFile(file=io.BytesIO(_zip({"package.json": PACKAGE, "server.js": server})), filename="app-v2.zip")
    asyncio.run(api.update_source(_request(), dep_id, file=upload))


# --- Pure helpers ---------------------------------------------------------------

def test_code_only_plans_are_recognised():
    assert code_update.is_code_only(CODE_ONLY_CHANGES)
    assert not code_update.is_code_only([])
    assert not code_update.is_code_only(CODE_ONLY_CHANGES + [{"address": "aws_lb.app", "action": "update"}])
    # The service may be updated in place, never replaced.
    assert not code_update.is_code_only([{"address": "aws_ecs_service.app", "action": "replace"}])


def test_tf_fingerprint_ignores_tfvars():
    a = {"main.tf": "x", "terraform.tfvars.json": '{"image_tag": "a"}'}
    b = {"main.tf": "x", "terraform.tfvars.json": '{"image_tag": "b"}'}
    assert code_update.tf_fingerprint(a) == code_update.tf_fingerprint(b)
    assert code_update.tf_fingerprint(a) != code_update.tf_fingerprint({"main.tf": "y"})
    assert code_update.tf_fingerprint({}) is None


def test_failed_verification_is_never_reused():
    rendered = {"main.tf": "x"}
    dep = {"code_update": {"previous_tf_sha256": code_update.tf_fingerprint(rendered),
                           "previous_verification": {"verdict": "FAIL"}}}
    assert code_update.reusable_verification(dep, rendered) is None
    dep["code_update"]["previous_verification"] = {"verdict": "PASS"}
    assert code_update.reusable_verification(dep, rendered)["reused_from_previous_release"] is True
    assert code_update.reusable_verification(dep, {"main.tf": "changed"}) is None


# --- End to end ---------------------------------------------------------------------

def test_code_update_chains_to_approval_with_a_code_only_plan(_env):
    dep_id = _deployed_app()
    before = store.get_deployment(dep_id)
    verify_calls = _env["calls"]["verify"]
    _env["dispatched"].clear()

    _push_update(dep_id)
    started = store.get_deployment(dep_id)
    assert started["status"] == DeployStatus.SOURCE_RECEIVED.value
    assert _env["dispatched"] == [("analyze", dep_id)]
    assert started["code_update"]["active"] is True
    assert started["code_update"]["previous_image_tag"] == before["build"]["image_tag"]
    assert started["plan_bundle_sha256"] is None and started["approved_by"] is None
    # Same stack: target, settings, AWS account and live outputs are kept.
    assert (started["target_type"], started["target_id"], started["settings"], started["outputs"]) == (
        before["target_type"], before["target_id"], before["settings"], before["outputs"])

    asyncio.run(pipeline.run_analysis(dep_id))  # chains build -> verify -> plan by itself

    after = store.get_deployment(dep_id)
    assert after["status"] == DeployStatus.AWAITING_APPROVAL.value
    assert after["build"]["image_tag"] != before["build"]["image_tag"]  # new code, new content-addressed image
    assert after["verification"]["reused_from_previous_release"] is True  # .tf files unchanged
    assert _env["calls"]["verify"] == verify_calls
    assert after["plan_summary"]["code_only"] is True
    assert after["is_destructive"] is False  # the task-definition revision doesn't need a destructive ack
    statuses = [e["to_status"] for e in store.list_events(dep_id)]
    assert statuses[-8:] == ["SOURCE_RECEIVED", "ANALYZING", "ANALYZED", "BUILDING", "VERIFYING", "VERIFIED",
                             "PLANNING", "AWAITING_APPROVAL"]


def test_infrastructure_changes_in_an_update_stay_destructive(_env):
    dep_id = _deployed_app()
    _env["calls"]["plan_changes"] = CODE_ONLY_CHANGES + [
        {"address": "aws_lb.app", "action": "replace", "type": "aws_lb"}]
    _push_update(dep_id)
    asyncio.run(pipeline.run_analysis(dep_id))
    after = store.get_deployment(dep_id)
    assert after["status"] == DeployStatus.AWAITING_APPROVAL.value
    assert after["plan_summary"]["code_only"] is False
    assert after["is_destructive"] is True


def test_update_is_refused_unless_deployed(_env):
    upload = UploadFile(file=io.BytesIO(_zip({"package.json": PACKAGE, "server.js": SERVER_V1})), filename="a.zip")
    dep_id = asyncio.run(api.upload_source(_request(), file=upload, region="us-east-1", environment="production")).deployment_id
    with pytest.raises(HTTPException) as exc:
        _push_update(dep_id)
    assert exc.value.status_code == 409


def test_an_update_that_no_longer_fits_the_target_fails_without_touching_aws(_env):
    dep_id = _deployed_app()
    upload = UploadFile(file=io.BytesIO(_zip({"index.html": "<h1>static now</h1>"})), filename="static.zip")
    asyncio.run(api.update_source(_request(), dep_id, file=upload))
    asyncio.run(pipeline.run_analysis(dep_id))
    after = store.get_deployment(dep_id)
    assert after["status"] == DeployStatus.FAILED.value
    assert "can no longer be deployed as ecs_service" in after["error"]
    assert after["applied_at"]  # the stack still exists, so another update can be tried
    assert code_update.can_update_code(after)
