"""Deployment mode: secret scan, project analyzer and decision engine.
Fixture projects are written to tmp_path; nothing in them is ever executed."""

import json

import pytest

from deploy.analyzer import analyze
from deploy.decision_engine import blocked_by_secrets, decide
from deploy.secret_scan import scan_source
from deploy.source_intake import ExtractedSource, SourceFile


def project(tmp_path, files):
    for path, content in files.items():
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    sizes = {path: (tmp_path / path).stat().st_size for path in files}
    return str(tmp_path), sorted(files), sizes


def profile_of(tmp_path, files):
    root, paths, sizes = project(tmp_path, files)
    return analyze(root, paths, sizes)


def source_of(tmp_path, files):
    root, paths, sizes = project(tmp_path, files)
    return ExtractedSource(root=root, files=[SourceFile(path=p, size=sizes[p], sha256="0" * 64) for p in paths])


# --- Secret scan -------------------------------------------------------------

def test_secret_scan_reports_location_and_kind_never_the_value(tmp_path):
    key = "AKIA" + "Q" * 16
    hits = scan_source(source_of(tmp_path, {
        "config.py": f"AWS_KEY = '{key}'\n",
        "deploy.pem": "-----BEGIN RSA PRIVATE KEY-----\nabc\n",
        "infra/terraform.tfstate": "{}",
        "settings.py": "token = 'ghp_" + "a" * 36 + "'\n",
        "README.md": "Example: AKIAIOSFODNN7EXAMPLE\n",
    }))
    assert {(h["path"], h["kind"]) for h in hits} == {
        ("config.py", "aws_access_key_id"), ("deploy.pem", "private_key"),
        ("infra/terraform.tfstate", "terraform_state"), ("settings.py", "github_token"),
    }
    assert key not in json.dumps(hits)


def test_secret_scan_ignores_minified_bundles_and_binaries(tmp_path):
    minified = "var a='x';" * 80 + "token=" + "A" * 60
    hits = scan_source(source_of(tmp_path, {"dist/app.js": minified, "index.html": "<p>ok</p>"}))
    assert hits == []


def test_secrets_block_every_target():
    decision = blocked_by_secrets()
    assert decision["blocked"] and decision["eligible"] == [] and decision["recommended"] is None


# --- Analyzer + decision -----------------------------------------------------

def test_plain_static_site(tmp_path):
    p = profile_of(tmp_path, {"index.html": "<h1>x</h1>", "style.css": "body{}"})
    assert p["runtime"] == "static" and p["static_output_dir"] == ""
    d = decide(p)
    assert d["eligible"] == ["static_site"] and d["recommended"] == "static_site"


def test_vite_project_with_committed_dist_is_static(tmp_path):
    p = profile_of(tmp_path, {
        "package.json": {"scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5.0.0"}},
        "index.html": "<div id=app></div>",  # vite's source template at the root
        "dist/index.html": "<h1>built</h1>", "src/main.js": "console.log(1)",
    })
    # The root index.html is vite's source template; the committed build wins.
    assert p["framework"] == "vite"
    assert p["static_output_dir"] == "dist"
    assert p["evidence"]["static_output_dir"] == [{"file": "dist/index.html", "rule": "static.output_dir"}]
    assert decide(p)["eligible"] == ["static_site"]


def test_vite_project_without_build_output_needs_a_build(tmp_path):
    p = profile_of(tmp_path, {
        "package.json": {"scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5.0.0"}},
        "index.html": "<div id=app></div>",
    })
    assert p["static_output_dir"] is None and p["build_required"] is True


def test_react_without_build_output_needs_a_build(tmp_path):
    p = profile_of(tmp_path, {
        "package.json": {"scripts": {"build": "react-scripts build"}, "dependencies": {"react": "18", "react-scripts": "5"}},
        "public/index.html": "<div id=root></div>", "src/App.js": "export default 1",
    })
    assert p["static_output_dir"] is None and p["build_required"] is True
    d = decide(p)
    assert d["eligible"] == []
    assert any(r["code"] == "build.unsafe_in_v1" for r in d["reasons"])


def test_python_lambda_handler(tmp_path):
    p = profile_of(tmp_path, {
        "app/main.py": "import json\n\ndef handler(event, context):\n    return {'statusCode': 200}\n",
        "requirements.txt": "requests==2.32.3\n# comment\n",
        ".python-version": "3.11.9\n",
    })
    assert p["runtime"] == "python" and p["lambda_handler"] == "app.main.handler"
    assert p["dependencies"] == ["requests"] and p["runtime_version"] == "3.11"
    assert p["evidence"]["lambda_handler"] == [{"file": "app/main.py", "rule": "python.def_handler"}]
    d = decide(p)
    assert d["eligible"] == ["lambda_http"] and d["recommended"] == "lambda_http"


def test_fastapi_with_mangum_is_lambda_compatible(tmp_path):
    p = profile_of(tmp_path, {
        "main.py": "from fastapi import FastAPI\nfrom mangum import Mangum\napp = FastAPI()\nhandler = Mangum(app)\n",
        "requirements.txt": "fastapi\nmangum\n",
    })
    assert p["lambda_handler"] == "main.handler" and p["framework"] == "fastapi"
    assert decide(p)["recommended"] == "lambda_http"


def test_flask_server_is_eligible_for_ecs_service(tmp_path):
    p = profile_of(tmp_path, {
        "app.py": "from flask import Flask\napp = Flask(__name__)\nif __name__ == '__main__':\n    app.run(port=5000)\n",
        "requirements.txt": "flask\n",
    })
    assert p["server_entrypoint"] and p["listens_on_port"] == 5000 and p["lambda_handler"] is None
    d = decide(p)
    assert d["eligible"] == ["ecs_service"] and d["recommended"] == "ecs_service"
    assert any(r["code"] == "server.container_detected" for r in d["reasons"])


def test_node_lambda_requires_lockfile_when_it_has_dependencies(tmp_path):
    files = {
        "package.json": {"name": "fn", "main": "index.js", "dependencies": {"uuid": "^9.0.0"}, "engines": {"node": ">=20"}},
        "index.js": "exports.handler = async (event) => ({ statusCode: 200 });\n",
    }
    p = profile_of(tmp_path, files)
    assert p["runtime"] == "node" and p["lambda_handler"] == "index.handler" and p["runtime_version"] == "20"
    assert any(r["code"] == "lambda.no_lockfile" for r in decide(p)["reasons"])
    assert decide(p)["eligible"] == []


def test_node_native_dependencies_are_not_eligible(tmp_path):
    p = profile_of(tmp_path, {
        "package.json": {"main": "index.js", "dependencies": {"sharp": "^0.33.0"}},
        "package-lock.json": {"lockfileVersion": 3},
        "index.js": "export const handler = async () => ({});\n",
    })
    assert p["native_dependencies"] == ["sharp"]
    assert any(r["code"] == "lambda.native_dependencies" for r in decide(p)["reasons"])


def test_express_server_and_dockerfile_is_eligible_for_ecs_service(tmp_path):
    p = profile_of(tmp_path, {
        "package.json": {"dependencies": {"express": "^4.19.0"}},
        "index.js": "const app = require('express')();\napp.listen(3000);\n",
        "Dockerfile": "FROM node:20\nEXPOSE 3000\n",
    })
    assert p["framework"] == "express" and p["server_entrypoint"] and p["has_dockerfile"]
    d = decide(p)
    assert d["eligible"] == ["ecs_service"] and d["recommended"] == "ecs_service"
    assert any(r["code"] == "container.dockerfile_detected" for r in d["reasons"])


def test_unrecognised_project(tmp_path):
    d = decide(profile_of(tmp_path, {"notes.txt": "hello"}))
    assert d["eligible"] == [] and d["reasons"][0]["code"] == "unknown.project_type"


@pytest.mark.parametrize("manifest", ["Pipfile"])
def test_unsupported_python_manifest(tmp_path, manifest):
    p = profile_of(tmp_path, {"handler.py": "def handler(event, context):\n    return 1\n", manifest: "[packages]\n"})
    assert p["dependency_manifest"] == "unsupported:Pipfile"
    assert any(r["code"] == "lambda.unsupported_manifest" for r in decide(p)["reasons"])
