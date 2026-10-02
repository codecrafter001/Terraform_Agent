"""Deployment mode builder and renderer: dependency specs are validated before
any install, install commands must pass check_build_argv, packages are
deterministic, and every project-derived value reaches Terraform only through
terraform.tfvars.json."""

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile

import pytest

import deploy.builder as builder
from deploy.analyzer import analyze
from deploy.builder import BuildError, build, check_build_argv, python_requirements, validate_npm_manifest
from deploy.renderer import TFVARS_FILENAME, render, template_files
from deploy.source_intake import ExtractedSource, SourceFile


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


def workdir(tmp_path, name="work"):
    d = tmp_path / name
    d.mkdir()
    return str(d)


# --- Dependency validation ---------------------------------------------------

def test_plain_requirements_are_accepted():
    assert python_requirements("requests==2.32.3\n\n# c\nboto3>=1.34,<2\nuvicorn[standard]\nx ; python_version >= '3.10'\n") == [
        "requests==2.32.3", "boto3>=1.34,<2", "uvicorn[standard]", "x ; python_version >= '3.10'",
    ]


@pytest.mark.parametrize("line", [
    "-r other.txt", "--index-url https://evil.example/simple", "--extra-index-url=https://x", "-e .",
    "git+https://github.com/a/b.git", "pkg @ https://evil.example/pkg.whl", "./local_pkg", "/abs/path.whl",
    "-f https://x", "--trusted-host x", "pkg==1.0 --hash=sha256:abc",
])
def test_requirements_with_options_urls_or_paths_are_refused(line):
    with pytest.raises(BuildError):
        python_requirements(f"requests\n{line}\n")


def test_npm_manifest_must_reference_only_the_public_registry():
    lock = {"lockfileVersion": 3, "packages": {"": {}, "node_modules/uuid": {"resolved": "https://registry.npmjs.org/uuid/-/uuid-9.0.1.tgz"}}}
    assert validate_npm_manifest({"dependencies": {"uuid": "^9.0.0", "@aws-sdk/client-s3": "3.x"}}, lock)
    for spec in ("git+https://github.com/a/b", "file:../x", "link:../x", "https://x/y.tgz", "github:a/b", "workspace:*", "npm:other@1"):
        with pytest.raises(BuildError):
            validate_npm_manifest({"dependencies": {"x": spec}}, lock)
    with pytest.raises(BuildError, match="outside registry"):
        validate_npm_manifest({"dependencies": {"uuid": "^9"}},
                              {"lockfileVersion": 3, "packages": {"node_modules/uuid": {"resolved": "https://evil.example/u.tgz"}}})
    with pytest.raises(BuildError, match="lockfileVersion"):
        validate_npm_manifest({"dependencies": {}}, {"lockfileVersion": 1})


# --- check_build_argv ---------------------------------------------------------

PIP_OK = [sys.executable, "-m", "pip", "install", "--only-binary=:all:", "--isolated", "--no-input",
          f"--index-url={builder.PYPI_INDEX}", "--target=/tmp/x", "--platform=manylinux2014_x86_64",
          "--implementation=cp", "--python-version=3.12", "--no-compile", "requests==2.32.3"]


def test_check_build_argv_accepts_only_the_safe_install_shapes():
    check_build_argv(PIP_OK)
    for bad in (
        [a for a in PIP_OK if a != "--only-binary=:all:"],                      # source builds -> setup.py runs
        PIP_OK + ["--extra-index-url=https://evil.example"],
        [a if not a.startswith("--index-url") else "--index-url=https://evil.example" for a in PIP_OK],
        PIP_OK + ["-r", "requirements.txt"],
        PIP_OK + ["git+https://github.com/a/b"],
        ["terraform", "apply"], ["tofu", "init"], ["sh", "-c", "pip install x"],
        ["npm", "install"], ["npm", "ci"],                                      # scripts not disabled
    ):
        with pytest.raises(ValueError, match="Safety Violation"):
            check_build_argv(bad)


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_check_build_argv_npm_shape():
    npm = shutil.which("npm")
    check_build_argv([npm, "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund", f"--registry={builder.NPM_REGISTRY}"])
    with pytest.raises(ValueError):
        check_build_argv([npm, "ci", "--omit=dev", "--no-audit", "--no-fund", f"--registry={builder.NPM_REGISTRY}"])


def test_build_env_has_no_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x" * 40)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    env = builder._build_env(str(tmp_path))
    assert not {"AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "DATABASE_URL"} & set(env)
    assert env["HOME"] == str(tmp_path)


# --- Builds -------------------------------------------------------------------

def test_static_build_collects_site_files(tmp_path):
    source, profile = make_source(tmp_path, {
        "package.json": json.dumps({"scripts": {"build": "vite build"}}), "index.html": "template",
        "dist/index.html": "<h1>x</h1>", "dist/assets/app.js": "console.log(1)", "dist/logo.svg": "<svg/>",
    })
    result = asyncio.run(build(source, profile, "static_site", workdir(tmp_path)))
    assert sorted(result.site_files) == ["assets/app.js", "index.html", "logo.svg"]
    assert result.site_files["assets/app.js"]["content_type"].startswith("text/javascript")
    assert result.site_files["logo.svg"]["content_type"] == "image/svg+xml"
    assert result.site_files["index.html"]["source"] == "site/index.html"
    assert (tmp_path / "work" / "site" / "index.html").read_text() == "<h1>x</h1>"


def test_lambda_without_dependencies_is_deterministic_and_runs_nothing(tmp_path, monkeypatch):
    def no_commands(*a, **k):
        raise AssertionError("no install command should run")

    monkeypatch.setattr(builder, "run_build_command", no_commands)
    files = {"handler.py": "def handler(event, context):\n    return {'statusCode': 200}\n", "lib/util.py": "X = 1\n"}
    source, profile = make_source(tmp_path, files)
    first = asyncio.run(build(source, profile, "lambda_http", workdir(tmp_path, "w1")))
    second = asyncio.run(build(source, profile, "lambda_http", workdir(tmp_path, "w2")))
    assert first.runtime == "python3.12" and first.handler == "handler.handler"
    assert first.package_sha256_b64 == second.package_sha256_b64
    with zipfile.ZipFile(tmp_path / "w1" / "artifacts" / "function.zip") as zf:
        assert zf.namelist() == ["handler.py", "lib/util.py"]
        assert {i.date_time for i in zf.infolist()} == {(1980, 1, 1, 0, 0, 0)}
    assert not (tmp_path / "w1" / ".build").exists()


def test_python_dependencies_install_wheels_only_through_the_checked_command(tmp_path, monkeypatch):
    captured = []

    async def fake_run(cmd, cwd, home, log):
        check_build_argv(cmd)  # the real chokepoint must accept what build() produces
        captured.append(cmd)

    monkeypatch.setattr(builder, "run_build_command", fake_run)
    source, profile = make_source(tmp_path, {
        "app.py": "def lambda_handler(event, context):\n    return 1\n", "requirements.txt": "requests==2.32.3\n",
        ".python-version": "3.11\n",
    })
    result = asyncio.run(build(source, profile, "lambda_http", workdir(tmp_path)))
    assert result.runtime == "python3.11"
    cmd = captured[0]
    assert "--only-binary=:all:" in cmd and "--python-version=3.11" in cmd and cmd[-1] == "requests==2.32.3"


def test_lambda_refuses_unsafe_requirements_before_running_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "run_build_command", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    source, profile = make_source(tmp_path, {
        "app.py": "def handler(event, context):\n    return 1\n", "requirements.txt": "-e git+https://x/y.git#egg=y\n",
    })
    with pytest.raises(BuildError, match="requirements.txt line 1"):
        asyncio.run(build(source, profile, "lambda_http", workdir(tmp_path)))


def test_lambda_package_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "MAX_LAMBDA_ZIP_BYTES", 100)
    source, profile = make_source(tmp_path, {"handler.py": "def handler(event, context):\n    return 1\n",
                                             "data.bin": os.urandom(5000)})
    with pytest.raises(BuildError, match="50 MB"):
        asyncio.run(build(source, profile, "lambda_http", workdir(tmp_path)))


# --- Renderer -----------------------------------------------------------------

def test_render_static_site_puts_every_value_in_tfvars_json(tmp_path):
    source, profile = make_source(tmp_path, {"index.html": "<h1>x</h1>", 'weird ${x} %{y}.html': "x"})
    wd = workdir(tmp_path)
    result = asyncio.run(build(source, profile, "static_site", wd))
    files = render("static_site", "dep-0123456789ab", "eu-west-1", "staging", {"spa_mode": True}, result, wd)
    assert set(files) == {"main.tf", "outputs.tf", "variables.tf", "versions.tf", TFVARS_FILENAME}
    for name, content in files.items():
        if name.endswith(".tf"):
            assert content == template_files("static_site")[name]  # templates are copied verbatim
            assert "weird" not in content
    tfvars = json.loads(files[TFVARS_FILENAME])
    assert tfvars["deployment_id"] == "dep-0123456789ab" and tfvars["spa_mode"] is True
    assert 'weird ${x} %{y}.html' in tfvars["site_files"]


def test_render_lambda_and_refuse_bad_ids_or_targets(tmp_path):
    source, profile = make_source(tmp_path, {"index.js": "exports.handler = async () => ({});\n", "package.json": "{}"})
    wd = workdir(tmp_path)
    result = asyncio.run(build(source, profile, "lambda_http", wd))
    files = render("lambda_http", "dep-0123456789ab", "us-east-1", "production", {"memory_mb": 512}, result, wd)
    tfvars = json.loads(files[TFVARS_FILENAME])
    assert tfvars["runtime"] == "nodejs20.x" and tfvars["handler"] == "index.handler" and tfvars["memory_mb"] == 512
    assert tfvars["package_file"] == "artifacts/function.zip" and tfvars["package_sha256_b64"]
    with pytest.raises(ValueError):
        render("lambda_http", "dep-../../x", "us-east-1", "production", {}, result, wd)
    with pytest.raises(ValueError):
        template_files("../agents")


def test_templates_have_no_provisioners_or_mutating_commands():
    from tools.hcl_invariants import MUTATING_COMMAND_PATTERN

    for target in ("static_site", "lambda_http", "ecs_service"):
        for name, content in template_files(target).items():
            assert "provisioner" not in content and 'data "external"' not in content, name
            assert not MUTATING_COMMAND_PATTERN.search(content), name


@pytest.mark.skipif(not os.getenv("TERRAAGENT_TEST_TERRAFORM") or shutil.which("terraform") is None,
                    reason="set TERRAAGENT_TEST_TERRAFORM=1 (downloads the AWS provider)")
@pytest.mark.parametrize("target", ["static_site", "lambda_http", "ecs_service"])
def test_templates_validate_with_terraform(target, tmp_path):
    for name, content in template_files(target).items():
        (tmp_path / name).write_text(content)
    for cmd in (["terraform", "fmt", "-check"], ["terraform", "init", "-backend=false", "-input=false"], ["terraform", "validate"]):
        proc = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr


def test_zip_dir_helper_used_for_bundles_is_readable(tmp_path):
    from deploy.pipeline import _zip_dir

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.txt").write_text("x")
    with zipfile.ZipFile(io.BytesIO(_zip_dir(str(tmp_path)))) as zf:
        assert zf.namelist() == ["a/b.txt"]
