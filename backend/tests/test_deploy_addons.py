"""Phase 4 add-ons for the full-stack target: detection (cache, S3 uploads, background
worker), what reaches terraform.tfvars.json (Aurora, cache, uploads, worker,
autoscaling), code-only updates of the worker, estimates, plan policy and the
apply session-policy size."""

import json

import pytest

from deploy.analyzer import analyze
from deploy.builder import BuildResult
from deploy.code_update import is_code_only
from deploy.estimates import PRESETS, estimate_fullstack
from deploy.fullstack import detect
from deploy.plan_policy import ALLOWED_TARGET_RESOURCES, MAX_RESOURCE_COUNT_BY_TARGET, evaluate_plan_policy
from deploy.renderer import tfvars
from deploy.sts import MAX_SESSION_POLICY_CHARS, apply_session_policy, plan_session_policy
from models.deployment import FullstackSettings

NODE_SERVER = "const app = require('express')();\napp.listen(process.env.PORT || 3000);\n"


def _layout(tmp_path, files):
    for path, content in files.items():
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    paths = sorted(files)
    sizes = {p: (tmp_path / p).stat().st_size for p in paths}
    profile = analyze(str(tmp_path), paths, sizes)
    return detect(str(tmp_path), paths, sizes, profile)


def _node_project(tmp_path, deps, scripts=None, extra=None):
    pkg = {"name": "app", "scripts": {"start": "node server.js", **(scripts or {})},
           "dependencies": {"express": "^4", **deps}}
    return _layout(tmp_path, {"package.json": json.dumps(pkg), "server.js": NODE_SERVER, **(extra or {})})


# --- Detection ------------------------------------------------------------------------

def test_detects_cache_uploads_and_worker_in_a_node_backend(tmp_path):
    layout = _node_project(tmp_path, {"ioredis": "^5", "multer-s3": "^3", "pg": "^8"}, scripts={"worker": "node worker.js"})
    assert layout["cache"]["evidence"][0]["rule"] == "cache.dep.ioredis"
    assert layout["object_storage"]["evidence"][0]["rule"] == "s3.dep.multer-s3"
    assert layout["worker"]["command"] == ["npm", "run", "worker"]


def test_plain_backend_has_no_addons(tmp_path):
    layout = _node_project(tmp_path, {})
    assert layout["cache"] is None and layout["object_storage"] is None and layout["worker"] is None


def test_celery_worker_command_points_at_the_app_module(tmp_path):
    layout = _layout(tmp_path, {
        "requirements.txt": "flask\ncelery\nredis\n",
        "app.py": "from flask import Flask\napp = Flask(__name__)\nif __name__ == '__main__':\n    app.run(host='0.0.0.0', port=5000)\n",
        "tasks.py": "from celery import Celery\ncelery_app = Celery('tasks')\n",
    })
    assert layout["worker"]["command"] == ["celery", "-A", "tasks:celery_app", "worker", "--loglevel=INFO"]
    assert layout["cache"] is not None


# --- tfvars -------------------------------------------------------------------------------

BUILD = BuildResult(kind="fullstack_source", container_port=3000, package_file="artifacts/source.zip", image_tag="src-0123456789abcdef")
LAYOUT = {"backend": {"dir": "", "port": 3000}, "frontend": None,
          "database": {"engine": "postgres", "rds_supported": True, "url_scheme": "postgresql"},
          "migration": None, "cache": {"evidence": []}, "object_storage": {"evidence": []},
          "worker": {"command": ["npm", "run", "worker"], "evidence": []},
          "env_keys": ["GEMINI_API_KEY", "REDIS_URL", "S3_BUCKET"]}


def _vars(**settings):
    return tfvars("fullstack_app", "dep-0123456789ab", "us-east-1", "production",
                  FullstackSettings(**settings).model_dump(), BUILD, {"fullstack": LAYOUT})


def test_addons_follow_detection_by_default():
    out = _vars()
    assert out["cache_enabled"] and out["uploads_bucket_enabled"]
    assert out["worker_command"] == ["npm", "run", "worker"]
    # REDIS_URL and S3_BUCKET are provided by the stack, so they're not asked for as secrets.
    assert out["secret_env_keys"] == ["GEMINI_API_KEY"]


def test_addons_can_be_switched_off():
    out = _vars(cache="none", uploads_bucket=False, worker_enabled=False)
    assert not out["cache_enabled"] and not out["uploads_bucket_enabled"] and out["worker_command"] == []
    assert out["secret_env_keys"] == ["GEMINI_API_KEY", "REDIS_URL", "S3_BUCKET"]


def test_aurora_and_autoscaling_settings():
    out = _vars(database="aurora", aurora_min_acu=0, aurora_max_acu=8, desired_count=2, autoscaling_max_count=6)
    assert (out["database_engine"], out["database_kind"], out["aurora_min_acu"], out["aurora_max_acu"]) == ("postgres", "aurora", 0.0, 8.0)
    assert out["autoscaling_max_count"] == 6
    assert _vars(desired_count=2)["autoscaling_max_count"] == 2  # no autoscaling: max == desired


def test_invalid_addon_settings_are_rejected():
    for bad in ({"aurora_min_acu": 3}, {"desired_count": 3, "autoscaling_max_count": 2}, {"autoscaling_cpu_target": 95}):
        with pytest.raises(ValueError):
            FullstackSettings(**bad)


# --- Code updates, estimates, plan policy, session policy ----------------------------------

def test_worker_revisions_count_as_code_only():
    assert is_code_only([
        {"address": "aws_s3_object.source", "action": "update"},
        {"address": "aws_ecs_task_definition.worker[0]", "action": "replace"},
        {"address": "aws_ecs_service.worker[0]", "action": "update"},
    ])


def test_estimate_includes_the_addons():
    est = estimate_fullstack({**PRESETS["production"], "database": "aurora", "aurora_min_acu": 0,
                              "autoscaling_max_count": 4}, LAYOUT)
    items = {line["item"]: line["monthly_usd"] for line in est["lines"]}
    cache = next(v for k, v in items.items() if k.startswith("Valkey"))
    assert 5 < cache < 8
    assert any(k.startswith("Aurora Serverless v2") for k in items)
    assert any(k.startswith("Background worker") for k in items)
    assert any("pauses after 5 idle minutes" in n for n in est["notes"])
    assert any("Autoscaling can add up to 2 more" in n for n in est["notes"])
    no_cache = estimate_fullstack({**PRESETS["production"], "cache": "none", "database": "none"}, LAYOUT)
    assert no_cache["minutes_high"] < est["minutes_high"]


def test_plan_policy_allows_the_addon_resources():
    allowed = ALLOWED_TARGET_RESOURCES["fullstack_app"]
    for rtype in ("aws_rds_cluster", "aws_rds_cluster_instance", "aws_elasticache_serverless_cache",
                  "aws_appautoscaling_target", "aws_appautoscaling_policy"):
        assert rtype in allowed
    assert MAX_RESOURCE_COUNT_BY_TARGET["fullstack_app"] == 150
    plan = {"resource_changes": [{"address": "aws_elasticache_serverless_cache.main[0]", "type": "aws_elasticache_serverless_cache",
                                  "change": {"actions": ["create"], "after": {"name": "terraagent-dep-0123456789ab"}}}]}
    assert evaluate_plan_policy(plan, "fullstack_app", "dep-0123456789ab")["passed"]
    assert not evaluate_plan_policy(plan, "ecs_service", "dep-0123456789ab")["passed"]


def test_session_policies_fit_the_aws_limit():
    args = ("terraagent-state-123456789012-ap-southeast-2", "target-0123456789ab", "dep-0123456789ab")
    assert len(plan_session_policy(*args)) <= MAX_SESSION_POLICY_CHARS
    apply = apply_session_policy(*args)
    assert len(apply) <= MAX_SESSION_POLICY_CHARS
    assert "application-autoscaling:*" in apply
