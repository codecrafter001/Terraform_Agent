"""Phase 3 service selection: presets, the cost/time estimate (deploy/estimates.py),
the settings that reach terraform.tfvars.json, and the estimate endpoint."""

import asyncio
import io
import json
import zipfile

import pytest
from fastapi import UploadFile
from starlette.requests import Request

import deploy.pipeline as pipeline
import routers.deployments as api
from deploy.artifacts import LocalArtifactStore
from deploy.builder import BuildResult
from deploy.estimates import PRESETS, estimate_fullstack, preset_for_environment
from deploy.renderer import tfvars
from models.deployment import FullstackSettings
from services.database import init_db
from services.rate_limiter import limiter

BACKEND_ONLY = {"backend": {"dir": "", "port": 3000}, "frontend": None,
                "database": {"engine": "postgres", "rds_supported": True, "url_scheme": "postgresql"},
                "env_keys": ["GEMINI_API_KEY"], "migration": None}
WITH_FRONTEND = {**BACKEND_ONLY, "frontend": {"dir": "client", "build_required": True}}


def _settings(preset: str, **overrides) -> dict:
    return FullstackSettings(**{**PRESETS[preset], "preset": preset, **overrides}).model_dump()


def test_every_preset_is_a_valid_settings_object():
    for name, values in PRESETS.items():
        FullstackSettings(**values, preset=name)


def test_preset_follows_the_environment():
    assert preset_for_environment("production") == "production"
    assert preset_for_environment("staging") == "staging"
    assert preset_for_environment("qa") == "dev"
    assert preset_for_environment(None) == "dev"


def test_dev_is_cheaper_and_skips_cloudfront():
    dev = estimate_fullstack(_settings("dev"), BACKEND_ONLY)
    prod = estimate_fullstack(_settings("production"), BACKEND_ONLY)
    assert dev["cdn"] is False and prod["cdn"] is True
    assert dev["monthly_usd"] < prod["monthly_usd"]
    assert any("plain HTTP" in n for n in dev["notes"])


def test_multi_az_doubles_the_database_line():
    single = estimate_fullstack(_settings("staging"), BACKEND_ONLY)
    multi = estimate_fullstack(_settings("staging", db_multi_az=True), BACKEND_ONLY)
    db = lambda e: next(line["monthly_usd"] for line in e["lines"] if "PostgreSQL" in line["item"])
    assert db(multi) == pytest.approx(2 * db(single), rel=0.01)
    assert multi["minutes_high"] > single["minutes_high"]


def test_no_database_is_faster_and_cheaper():
    with_db = estimate_fullstack(_settings("dev"), BACKEND_ONLY)
    without = estimate_fullstack(_settings("dev", database="none"), BACKEND_ONLY)
    assert without["minutes_high"] < with_db["minutes_high"]
    assert without["monthly_usd"] < with_db["monthly_usd"]


def test_a_separate_frontend_keeps_cloudfront_on():
    est = estimate_fullstack(_settings("dev"), WITH_FRONTEND)
    assert est["cdn"] is True
    assert any("CloudFront stays on" in n for n in est["notes"])


def test_secret_count_includes_the_database_password_and_external_url():
    rds = estimate_fullstack(_settings("dev"), BACKEND_ONLY)
    assert any(line["item"] == "Secrets Manager (2 secrets)" for line in rds["lines"])  # GEMINI_API_KEY + RDS
    external = estimate_fullstack(_settings("dev", database="external"), BACKEND_ONLY)
    assert any(line["item"] == "Secrets Manager (2 secrets)" for line in external["lines"])  # + DATABASE_URL


def test_preset_values_reach_tfvars():
    build = BuildResult(kind="fullstack_source", container_port=3000, package_file="artifacts/source.zip",
                        image_tag="src-0123456789abcdef")
    out = tfvars("fullstack_app", "dep-0123456789ab", "us-east-1", "development",
                 _settings("dev"), build, {"fullstack": BACKEND_ONLY})
    assert (out["cdn_enabled"], out["db_backup_retention_days"], out["db_final_snapshot"]) == (False, 1, False)
    prod = tfvars("fullstack_app", "dep-0123456789ab", "us-east-1", "production",
                  _settings("production"), build, {"fullstack": BACKEND_ONLY})
    assert (prod["cdn_enabled"], prod["db_backup_retention_days"], prod["db_final_snapshot"]) == (True, 7, True)
    assert prod["desired_count"] == 2


def test_backup_days_are_bounded():
    with pytest.raises(ValueError):
        FullstackSettings(db_backup_retention_days=36)


# --- Endpoint -------------------------------------------------------------------

@pytest.fixture
def _env(tmp_path, monkeypatch):
    init_db()
    monkeypatch.setattr(limiter, "enabled", False)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    for module in (api, pipeline):
        monkeypatch.setattr(module, "get_artifact_store", lambda: artifacts)
    monkeypatch.setattr(api, "_dispatch", lambda stage, dep_id: None)


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""})


def test_estimate_endpoint_uses_the_analysed_layout(_env):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("package.json", json.dumps({"name": "a", "scripts": {"start": "node server.js"},
                                                "dependencies": {"express": "^4", "pg": "^8"}}))
        zf.writestr("server.js", "const app = require('express')();\napp.listen(process.env.PORT || 3000);\n")
    upload = UploadFile(file=io.BytesIO(buf.getvalue()), filename="app.zip")
    dep_id = asyncio.run(api.upload_source(_request(), file=upload, region="us-east-1", environment="staging")).deployment_id
    asyncio.run(pipeline.run_analysis(dep_id))

    result = asyncio.run(api.estimate_deployment(_request(), dep_id, FullstackSettings(**PRESETS["staging"])))
    assert result["suggested_preset"] == "staging"
    assert set(result["presets"]) == {"dev", "staging", "production"}
    assert any("PostgreSQL" in line["item"] for line in result["estimate"]["lines"])  # pg was detected
    assert result["estimate"]["minutes_low"] > 0
