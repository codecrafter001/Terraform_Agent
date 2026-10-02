"""Tests for Phase 5.3: ECS Fargate + ALB Container Target.

Validates:
1. Decision engine detects Dockerfile or port listener and recommends ecs_service.
2. Builder packages deterministic source.zip for CodeBuild (Docker build isolation).
3. Renderer produces valid terraform.tfvars.json and verbatim template files.
4. Plan policy validates ECS, ECR, CodeBuild, and ALB resource types.
5. Template conforms to security invariants (no provisioners, no local-exec).
"""

import asyncio
import json
import os
import zipfile
import pytest

from deploy.analyzer import analyze
from deploy.builder import build
from deploy.decision_engine import decide, DECISION_RULES_VERSION
from deploy.plan_policy import evaluate_plan_policy
from deploy.renderer import render, template_files, TFVARS_FILENAME
from deploy.source_intake import ExtractedSource, SourceFile
from tools.hcl_invariants import MUTATING_COMMAND_PATTERN


def make_source(tmp_path, files):
    root = tmp_path / "src"
    for path, content in files.items():
        p = root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
    paths = sorted(files)
    sizes = {p: (root / p).stat().st_size for p in paths}
    source = ExtractedSource(root=str(root), files=[SourceFile(p, sizes[p], "0" * 64) for p in paths])
    return source, analyze(str(root), paths, sizes)


def test_ecs_decision_rules_version():
    assert DECISION_RULES_VERSION == 2


def test_dockerfile_triggers_ecs_eligibility_and_recommendation(tmp_path):
    files = {
        "Dockerfile": "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nEXPOSE 8000\nCMD [\"python\", \"app.py\"]\n",
        "app.py": "print('hello')\n",
    }
    source, profile = make_source(tmp_path, files)
    assert profile["has_dockerfile"] is True
    assert profile["listens_on_port"] == 8000
    
    decision = decide(profile)
    assert "ecs_service" in decision["eligible"]
    assert decision["recommended"] == "ecs_service"
    assert any(r["code"] == "container.dockerfile_detected" for r in decision["reasons"])


def test_server_entrypoint_without_dockerfile_triggers_ecs(tmp_path):
    files = {
        "app.py": "from flask import Flask\napp = Flask(__name__)\nif __name__ == '__main__':\n    app.run(port=5000)\n",
        "requirements.txt": "flask\n",
    }
    source, profile = make_source(tmp_path, files)
    assert profile["server_entrypoint"] is True
    assert profile["lambda_handler"] is None
    
    decision = decide(profile)
    assert "ecs_service" in decision["eligible"]
    assert decision["recommended"] == "ecs_service"
    assert any(r["code"] == "server.container_detected" for r in decision["reasons"])


def test_ecs_builder_produces_deterministic_source_bundle(tmp_path):
    files = {
        "Dockerfile": "FROM node:20-alpine\nCOPY . .\nEXPOSE 3000\n",
        "server.js": "const http = require('http');\n",
        "package.json": "{\"name\":\"app\"}\n",
    }
    source, profile = make_source(tmp_path, files)
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    
    result = asyncio.run(build(source, profile, "ecs_service", workdir))
    assert result.kind == "container_source"
    assert result.container_port == 3000
    assert result.package_file == "artifacts/source.zip"
    assert result.package_sha256_b64 is not None
    assert result.package_bytes > 0
    
    zip_path = os.path.join(workdir, "artifacts", "source.zip")
    assert os.path.exists(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        assert sorted(zf.namelist()) == ["Dockerfile", "package.json", "server.js"]
        for info in zf.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)


def test_render_ecs_service_generates_correct_tfvars(tmp_path):
    files = {
        "Dockerfile": "FROM python:3.12\nEXPOSE 8080\n",
        "main.py": "print('ok')\n",
    }
    source, profile = make_source(tmp_path, files)
    workdir = str(tmp_path / "work")
    os.makedirs(workdir, exist_ok=True)
    
    result = asyncio.run(build(source, profile, "ecs_service", workdir))
    settings = {
        "container_port": 8080,
        "cpu": 512,
        "memory_mb": 1024,
        "desired_count": 2,
        "image_tag": "v1.0.0",
        "certificate_arn": "arn:aws:acm:us-east-1:123456789012:certificate/abc-123",
    }
    rendered = render("ecs_service", "dep-0123456789ab", "us-east-1", "production", settings, result, workdir)
    assert set(rendered.keys()) == {"main.tf", "outputs.tf", "variables.tf", "versions.tf", TFVARS_FILENAME}
    
    tfvars_data = json.loads(rendered[TFVARS_FILENAME])
    assert tfvars_data["deployment_id"] == "dep-0123456789ab"
    assert tfvars_data["region"] == "us-east-1"
    assert tfvars_data["container_port"] == 8080
    assert tfvars_data["cpu"] == 512
    assert tfvars_data["memory_mb"] == 1024
    assert tfvars_data["desired_count"] == 2
    assert tfvars_data["image_tag"] == "v1.0.0"
    assert tfvars_data["certificate_arn"] == "arn:aws:acm:us-east-1:123456789012:certificate/abc-123"


def test_ecs_plan_policy_evaluation():
    plan_json = {
        "resource_changes": [
            {
                "address": "aws_ecr_repository.app",
                "type": "aws_ecr_repository",
                "change": {"actions": ["create"], "after": {"name": "terraagent-dep-0123456789ab"}},
            },
            {
                "address": "aws_codebuild_project.builder",
                "type": "aws_codebuild_project",
                "change": {"actions": ["create"], "after": {"name": "terraagent-dep-0123456789ab"}},
            },
            {
                "address": "aws_ecs_cluster.app",
                "type": "aws_ecs_cluster",
                "change": {"actions": ["create"], "after": {"name": "terraagent-dep-0123456789ab"}},
            },
            {
                "address": "aws_ecs_service.app",
                "type": "aws_ecs_service",
                "change": {"actions": ["create"], "after": {"name": "terraagent-dep-0123456789ab"}},
            },
            {
                "address": "aws_lb.app",
                "type": "aws_lb",
                "change": {"actions": ["create"], "after": {"name": "terraagent-dep-0123456789ab"}},
            },
            {
                "address": "aws_security_group.alb",
                "type": "aws_security_group",
                "change": {"actions": ["create"], "after": {"name": "terraagent-dep-0123456789ab-alb"}},
            },
        ]
    }
    result = evaluate_plan_policy(plan_json, target_type="ecs_service", deployment_id="dep-0123456789ab")
    assert result["passed"] is True
    assert result["violations"] == []
    assert result["is_destructive"] is False
    assert result["summary"]["resource_count"] == 6


def test_ecs_template_hcl_invariants():
    files = template_files("ecs_service")
    for name, content in files.items():
        assert "provisioner" not in content, f"provisioner in {name}"
        assert 'data "external"' not in content, f'data "external" in {name}'
        assert not MUTATING_COMMAND_PATTERN.search(content), f"Mutating command pattern in {name}"
