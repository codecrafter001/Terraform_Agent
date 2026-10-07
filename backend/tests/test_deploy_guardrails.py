"""Guardrails for deployment mode (design doc §10.2) that must hold in every
phase: migration mode can't reach it, it can't reach the terraform binary
except through TerraformRunner, it touches no AWS API in Phases 1-2, and its
Celery tasks carry nothing but a deployment id."""

import ast
import inspect
import os

import deploy.tasks as tasks
from tools.terraform_runner import ALLOWED_SUBCOMMANDS, BLOCKED_WORDS

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(BACKEND, "deploy")


def _py_files(directory):
    for dirpath, _, filenames in os.walk(directory):
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _imports(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_terraform_runner_allowlist_is_unchanged():
    # Deployment mode must never widen the migration-mode chokepoint.
    assert ALLOWED_SUBCOMMANDS == frozenset({"version", "fmt", "init", "validate", "plan", "show", "providers"})
    assert BLOCKED_WORDS == frozenset({"apply", "destroy", "import"})


def test_migration_mode_never_imports_deploy():
    for directory in ("agents", "tools"):
        for path in _py_files(os.path.join(BACKEND, directory)):
            assert not any(m == "deploy" or m.startswith("deploy.") for m in _imports(path)), path
    assert not any(m.startswith("deploy") for m in _imports(os.path.join(BACKEND, "routers", "scan.py")))


def test_deploy_never_uses_boto_or_spawns_terraform_itself():
    for path in _py_files(DEPLOY):
        modules = set(_imports(path))
        basename = os.path.basename(path)
        # S3ArtifactStore in artifacts.py uses boto3 for encrypted S3 artifact storage (Phase 6 D6);
        # preflight.py uses it for read-only pre-plan checks (asserted below).
        disallowed = {m for m in modules if m.split(".")[0] in ("boto3", "botocore", "subprocess")}
        if basename in ("artifacts.py", "preflight.py"):
            disallowed = {m for m in disallowed if m.split(".")[0] == "subprocess"}
        assert not disallowed, path
        source = open(path, encoding="utf-8").read()
        if "create_subprocess_exec" in source:
            # Only builder.py (behind check_build_argv) and apply_runner.py (behind check_apply_argv) spawn processes
            assert basename in ("builder.py", "apply_runner.py"), path
            if basename == "builder.py":
                assert "check_build_argv(cmd)" in source
            elif basename == "apply_runner.py":
                assert "check_apply_argv(apply_cmd" in source


def test_preflight_only_reads_from_aws():
    """deploy/preflight.py is the one deploy module that calls AWS APIs itself: only
    read operations (describe_*/get_*, plus the client/paginator plumbing)."""
    tree = ast.parse(open(os.path.join(DEPLOY, "preflight.py"), encoding="utf-8").read())
    plumbing = {"client", "Session", "paginate", "get", "getLogger", "warning", "Config"}
    calls = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert calls, "no attribute calls found - the check isn't looking at anything"
    writes = {c for c in calls if c not in plumbing and not c.startswith(("describe_", "get_"))}
    assert not writes, f"preflight.py calls non-read operations: {writes}"


def test_deploy_has_no_apply_destroy_or_import_strings():
    for path in _py_files(DEPLOY):
        source = open(path, encoding="utf-8").read()
        # -auto-approve is strictly forbidden everywhere
        assert "-auto-approve" not in source, f"-auto-approve found in {path}"

        basename = os.path.basename(path)
        # Outside of apply_runner (the one apply chokepoint) and plan_policy (plan action labels), no apply/destroy/import in argv shapes
        if basename in ("apply_runner.py", "plan_policy.py"):
            continue

        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple)):
                elements = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                has_tf = any(e in ("terraform", "tofu") for e in elements)
                has_blocked = any(e in ("apply", "destroy", "import", "-destroy") for e in elements)
                assert not (has_tf and has_blocked), f"Forbidden terraform mutating command list in {path}: {elements}"


def test_celery_tasks_take_only_a_deployment_id():
    for task in (tasks.analyze_task, tasks.build_and_verify_task, tasks.plan_task, tasks.apply_task):
        params = list(inspect.signature(task.run).parameters)
        assert params == ["deployment_id"], task.name
    assert inspect.signature(tasks.sweep_task.run).parameters == {}


