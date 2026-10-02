"""Deployment mode end to end without a broker: the state machine, the two
pipeline stages and the API routes (called directly, like test_jobs_archive,
so the in-memory SQLite database stays on one thread). Terraform and the
scanners are replaced by a fake verify(); the builder runs for real."""

import asyncio
import io
import zipfile
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException, UploadFile
from starlette.requests import Request

import deploy.pipeline as pipeline
import routers.deployments as api
from deploy import store
from deploy.artifacts import LocalArtifactStore
from deploy.store import DeployStatus, InvalidTransition
from models.deployment import GitHubSourceRequest, LambdaSettings, PrepareLambda, PrepareStaticSite
from services.database import init_db
from services.rate_limiter import limiter


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    init_db()
    monkeypatch.setattr(limiter, "enabled", False)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    for module in (api, pipeline):
        monkeypatch.setattr(module, "get_artifact_store", lambda: artifacts)
    dispatched = []
    monkeypatch.setattr(api, "_dispatch", lambda stage, dep_id: dispatched.append((stage, dep_id)))

    async def fake_verify(deployment_id, files, binary="terraform"):
        return {"verdict": "INCOMPLETE", "incomplete_reasons": ["checkov is not installed"],
                "validation": {"passed": True, "checks": []}, "security": {"findings": []},
                "security_posture": {"score": None}, "cost": {"tool_skipped": True}}

    monkeypatch.setattr(pipeline, "verify", fake_verify)
    return dispatched


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""})


def _zip(files) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _upload(files, region="us-east-1") -> str:
    upload = UploadFile(file=io.BytesIO(_zip(files)), filename="site.zip")
    return asyncio.run(api.upload_source(_request(), file=upload, region=region, environment="staging")).deployment_id


# --- State machine ------------------------------------------------------------

def test_transitions_are_checked_and_audited():
    dep_id = store.new_deployment_id()
    store.create_deployment(dep_id, "zip", "x.zip", "us-east-1", "production")
    with pytest.raises(InvalidTransition):
        store.transition(dep_id, DeployStatus.VERIFIED)
    store.transition(dep_id, DeployStatus.ANALYZING)
    store.transition(dep_id, DeployStatus.FAILED, reason="boom", error="boom")
    with pytest.raises(InvalidTransition):  # FAILED -> BUILDING only from an allowed set the caller names
        store.transition(dep_id, DeployStatus.BUILDING, allowed_from=frozenset({DeployStatus.ANALYZED}))
    events = store.list_events(dep_id)
    assert [(e["from_status"], e["to_status"]) for e in events] == [
        (None, "SOURCE_RECEIVED"), ("SOURCE_RECEIVED", "ANALYZING"), ("ANALYZING", "FAILED")]
    assert store.get_deployment(dep_id)["completed_at"]


def test_stale_in_progress_deployments_are_found():
    dep_id = store.new_deployment_id()
    store.create_deployment(dep_id, "zip", "x.zip", "us-east-1", "production")
    store.transition(dep_id, DeployStatus.ANALYZING)
    assert dep_id not in store.stale_in_progress(60)
    assert dep_id in store.stale_in_progress(60, now=datetime.utcnow() + timedelta(hours=1))


# --- Upload -> analyze -> prepare -> build/verify -----------------------------

def test_static_site_flow(_env):
    dep_id = _upload({"index.html": "<h1>hello</h1>", "css/app.css": "body{}", ".env": "SECRET=1"})
    assert _env == [("analyze", dep_id)]
    assert store.get_deployment(dep_id)["source_artifact_id"]

    asyncio.run(pipeline.run_analysis(dep_id))
    detail = asyncio.run(api.get_deployment_detail(dep_id))
    assert detail.status == "ANALYZED" and detail.can_prepare
    assert detail.decision["recommended"] == "static_site" and detail.target_type == "static_site"
    assert {"path": ".env", "reason": "environment file (may hold secrets; never packaged)"} in detail.intake["dropped"]

    with pytest.raises(HTTPException) as e:
        asyncio.run(api.prepare_deployment(_request(), dep_id, PrepareLambda(target="lambda_http")))
    assert e.value.status_code == 422  # not an eligible target

    asyncio.run(api.prepare_deployment(_request(), dep_id, PrepareStaticSite(target="static_site")))
    assert _env[-1] == ("build", dep_id)
    with pytest.raises(HTTPException) as e:  # already building
        asyncio.run(api.prepare_deployment(_request(), dep_id, PrepareStaticSite(target="static_site")))
    assert e.value.status_code == 409

    asyncio.run(pipeline.run_build_and_verify(dep_id))
    detail = asyncio.run(api.get_deployment_detail(dep_id))
    assert detail.status == "VERIFIED" and detail.verdict == "INCOMPLETE"
    assert detail.build["file_count"] == 2
    assert "terraform.tfvars.json" in detail.rendered_files
    tf = asyncio.run(api.get_rendered_terraform(dep_id))
    assert '"index.html"' in tf.files["terraform.tfvars.json"]
    assert [e.to_status for e in detail.events][-3:] == ["BUILDING", "VERIFYING", "VERIFIED"]

    # Re-build with other settings is allowed from VERIFIED.
    asyncio.run(api.prepare_deployment(_request(), dep_id,
                                       PrepareStaticSite(target="static_site", settings={"spa_mode": True})))
    assert store.get_deployment(dep_id)["settings"]["spa_mode"] is True


def test_lambda_flow_with_settings():
    dep_id = _upload({"handler.py": "def handler(event, context):\n    return {'statusCode': 200}\n"})
    asyncio.run(pipeline.run_analysis(dep_id))
    asyncio.run(api.prepare_deployment(_request(), dep_id,
                                       PrepareLambda(target="lambda_http", settings=LambdaSettings(memory_mb=512))))
    asyncio.run(pipeline.run_build_and_verify(dep_id))
    dep = store.get_deployment(dep_id)
    assert dep["status"] == "VERIFIED" and dep["build"]["runtime"] == "python3.12"
    assert '"memory_mb": 512' in dep["rendered"]["terraform.tfvars.json"]


def test_secrets_fail_the_deployment_and_are_not_echoed():
    key = "AKIA" + "Z" * 16
    dep_id = _upload({"index.html": "x", "config.js": f"const k = '{key}';"})
    asyncio.run(pipeline.run_analysis(dep_id))
    dep = store.get_deployment(dep_id)
    assert dep["status"] == "FAILED" and "config.js:1" in dep["error"]
    assert dep["decision"]["blocked"] and dep["intake"]["secret_hits"][0]["kind"] == "aws_access_key_id"
    assert key not in str(dep)
    detail = asyncio.run(api.get_deployment_detail(dep_id))
    assert not detail.can_prepare


def test_bad_archive_fails_cleanly():
    dep_id = _upload({"../escape.txt": "x"})
    asyncio.run(pipeline.run_analysis(dep_id))
    dep = store.get_deployment(dep_id)
    assert dep["status"] == "FAILED" and "path traversal" in dep["error"]


def test_build_errors_fail_without_crashing():
    dep_id = _upload({"handler.py": "def handler(event, context):\n    return 1\n", "requirements.txt": "-r other.txt\n"})
    asyncio.run(pipeline.run_analysis(dep_id))
    asyncio.run(api.prepare_deployment(_request(), dep_id, PrepareLambda(target="lambda_http")))
    asyncio.run(pipeline.run_build_and_verify(dep_id))
    dep = store.get_deployment(dep_id)
    assert dep["status"] == "FAILED" and "requirements.txt line 1" in dep["error"]
    assert asyncio.run(api.get_deployment_detail(dep_id)).can_prepare  # can retry after fixing settings


def test_upload_validation():
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.upload_source(_request(), file=UploadFile(file=io.BytesIO(b"nope"), filename="x.zip"),
                                      region="us-east-1", environment="p"))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.upload_source(_request(), file=UploadFile(file=io.BytesIO(_zip({"a": "b"})), filename="x.zip"),
                                      region="mars-1", environment="p"))
    assert e.value.status_code == 422


def test_upload_size_cap(monkeypatch):
    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(HTTPException) as e:
        _upload({"index.html": "x" * 100})
    assert e.value.status_code == 413


def test_github_source_token_is_used_once_and_never_stored(monkeypatch):
    seen = {}

    async def fake_download(repo, ref, token):
        seen.update(repo=repo, ref=ref, token=token)
        return _zip({"repo-abc/index.html": "x"})

    monkeypatch.setattr(api, "download_github_archive", fake_download)
    body = GitHubSourceRequest(repo="https://github.com/acme/site", ref="main", github_token="ghp_" + "s" * 36)
    dep_id = asyncio.run(api.github_source(_request(), body)).deployment_id
    assert seen == {"repo": "acme/site", "ref": "main", "token": "ghp_" + "s" * 36}
    dep = store.get_deployment(dep_id)
    assert dep["source_name"] == "acme/site@main"
    assert "ghp_" not in str(dep) and "ghp_" not in str(store.list_events(dep_id))


def test_unknown_deployment_is_404():
    for call in (api.get_deployment_detail("dep-000000000000"), api.get_rendered_terraform("dep-000000000000")):
        with pytest.raises(HTTPException) as e:
            asyncio.run(call)
        assert e.value.status_code == 404
