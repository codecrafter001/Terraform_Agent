"""Builder (design doc §9.2): turns an analysed source tree into the artifact a
template deploys - a static site directory or a Lambda zip.

No project code runs here. Python dependencies are installed wheels-only
(`--only-binary=:all:`, so no setup.py), for Lambda's platform; Node
dependencies with `npm ci --ignore-scripts` (no lifecycle scripts). Every
dependency spec is validated first (registry packages only: no URLs, paths,
VCS sources or pip options smuggled through requirements.txt), every command
must pass check_build_argv, and subprocesses get a minimal environment with
no credentials. This is a chokepoint for pip/npm the same way
terraform_runner.check_argv is for terraform: nothing here may ever run the
terraform/tofu binary.
"""

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import tomllib
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from deploy.analyzer import ProjectProfile
from deploy.config import BUILD_TIMEOUT_SECONDS, MAX_LAMBDA_ZIP_BYTES, MAX_STATIC_FILES
from deploy.dockerfiles import DockerfileError
from deploy.dockerfiles import generate as generate_container
from deploy.source_intake import ExtractedSource
from tools.credential_scrubber import CredentialScrubber
from tools.subprocess_exec import run_exec

PYPI_INDEX = "https://pypi.org/simple"
NPM_REGISTRY = "https://registry.npmjs.org/"

PYTHON_RUNTIMES = {"3.10": "python3.10", "3.11": "python3.11", "3.12": "python3.12", "3.13": "python3.13"}
DEFAULT_PYTHON = "3.12"
NODE_RUNTIMES = {"20": "nodejs20.x", "22": "nodejs22.x"}
DEFAULT_NODE = "20"

# name[extras] (specifier list)? (; marker)?  - no URLs, paths, options or VCS.
_PY_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?"
    r"(\s*(===|==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9.*+!_-]+(\s*,\s*(===|==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9.*+!_-]+)*)?"
    r"(\s*;\s*[A-Za-z0-9_ .'\"<>=!,()]+)?$"
)
# Semver ranges and dist-tags only - never git/http/file/link/workspace/npm: aliases.
_NPM_SPEC = re.compile(r"^[A-Za-z0-9 ^~<>=*.|+-]{1,100}$")
_NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8", ".json": "application/json",
    ".map": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf", ".wasm": "application/wasm",
    ".txt": "text/plain; charset=utf-8", ".xml": "application/xml", ".webmanifest": "application/manifest+json",
    ".pdf": "application/pdf", ".mp4": "video/mp4", ".webm": "video/webm",
}
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


class BuildError(RuntimeError):
    """The build can't proceed. The message is safe to show the user."""


@dataclass
class BuildResult:
    kind: str  # static_site | lambda_zip | container_source
    runtime: Optional[str] = None
    handler: Optional[str] = None
    container_port: Optional[int] = None
    site_files: Dict[str, Dict[str, str]] = field(default_factory=dict)  # key -> {source, content_type, md5}
    package_file: Optional[str] = None  # relative to the workdir
    package_sha256_b64: Optional[str] = None
    package_bytes: int = 0
    image_tag: Optional[str] = None  # container targets: content-addressed tag of source.zip
    generated_files: List[str] = field(default_factory=list)  # files TerraAgent added to source.zip
    start_command: List[str] = field(default_factory=list)
    log: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "runtime": self.runtime, "handler": self.handler,
            "container_port": self.container_port,
            "file_count": len(self.site_files), "package_bytes": self.package_bytes,
            "package_sha256_b64": self.package_sha256_b64, "warnings": self.warnings,
            "image_tag": self.image_tag, "generated_files": self.generated_files,
            "start_command": self.start_command,
            "log": [CredentialScrubber.scrub_text(line) for line in self.log[-200:]],
        }


# --- Validation -----------------------------------------------------------

def python_requirements(text: str) -> List[str]:
    """Validated requirement strings from a requirements.txt, or BuildError."""
    reqs: List[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") or not _PY_REQUIREMENT.match(line) or "://" in line or "@" in line:
            raise BuildError(
                f"requirements.txt line {lineno} isn't a plain package requirement. Only 'name', 'name==1.2' style "
                "lines are supported (no -r/-e/--index-url options, URLs, paths or VCS sources)."
            )
        reqs.append(line)
    return reqs


def validate_npm_manifest(package: Dict[str, Any], lock: Dict[str, Any]) -> Dict[str, str]:
    """The production dependencies, after checking that package.json and
    package-lock.json only reference packages from the public npm registry."""
    deps = package.get("dependencies") or {}
    if not isinstance(deps, dict):
        raise BuildError("package.json 'dependencies' must be an object")
    for name, spec in deps.items():
        if not _NPM_NAME.match(str(name)) or not _NPM_SPEC.match(str(spec)):
            raise BuildError(
                f"Dependency '{str(name)[:80]}' uses an unsupported source ({str(spec)[:80]!r}). Only npm registry "
                "versions are supported (no git, http, file, link or workspace dependencies)."
            )
    if int(lock.get("lockfileVersion") or 0) < 2:
        raise BuildError("package-lock.json must be lockfileVersion 2 or later (npm 7+)")
    for path, entry in (lock.get("packages") or {}).items():
        if not isinstance(entry, dict):
            continue
        resolved = entry.get("resolved")
        if resolved and not str(resolved).startswith(NPM_REGISTRY):
            raise BuildError(f"package-lock.json resolves '{str(path)[:80]}' from outside registry.npmjs.org")
        if entry.get("link"):
            raise BuildError(f"package-lock.json links '{str(path)[:80]}' to a local path")
    return {str(k): str(v) for k, v in deps.items()}


def check_build_argv(cmd: List[str]) -> None:
    """Raise unless cmd is exactly one of the two install shapes this module builds."""
    if len(cmd) >= 4 and cmd[0] == sys.executable and cmd[1:4] == ["-m", "pip", "install"]:
        flags = [a for a in cmd[4:] if a.startswith("-")]
        required = {"--only-binary=:all:", "--isolated", "--no-input", f"--index-url={PYPI_INDEX}"}
        if not required.issubset(flags):
            raise ValueError("Safety Violation: pip install is missing a required safety flag")
        allowed_prefixes = (
            "--only-binary=:all:", "--isolated", "--no-input", f"--index-url={PYPI_INDEX}", "--target=",
            "--platform=", "--implementation=cp", "--python-version=", "--no-compile", "--disable-pip-version-check",
            "--no-cache-dir", "--upgrade",
        )
        for flag in flags:
            if not flag.startswith(allowed_prefixes):
                raise ValueError(f"Safety Violation: pip flag '{flag}' is not allowed")
            if flag.startswith("--index-url") and flag != f"--index-url={PYPI_INDEX}":
                raise ValueError("Safety Violation: only the public PyPI index is allowed")
        for arg in cmd[4:]:
            if not arg.startswith("-") and not _PY_REQUIREMENT.match(arg):
                raise ValueError(f"Safety Violation: '{arg[:80]}' is not a plain requirement")
        return
    npm = shutil.which("npm")
    expected = ["ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund", f"--registry={NPM_REGISTRY}"]
    if npm and len(cmd) == 1 + len(expected) and cmd[0] == npm and cmd[1:] == expected:
        return
    raise ValueError(f"Safety Violation: '{os.path.basename(cmd[0]) if cmd else ''}' is not an allowed build command")


# --- Execution ------------------------------------------------------------

def _build_env(home: str) -> Dict[str, str]:
    """Only what pip/npm need to run - never os.environ.copy() (no AWS keys,
    tokens or service secrets reach a build)."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "TMPDIR": home, "TEMP": home, "TMP": home,
           "PYTHONNOUSERSITE": "1", "npm_config_userconfig": os.path.join(home, ".npmrc"),
           "npm_config_globalconfig": os.path.join(home, ".npmrc-global"), "npm_config_cache": os.path.join(home, ".npm")}
    for name in ("SYSTEMROOT", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    for cfg in ("npm_config_userconfig", "npm_config_globalconfig"):
        open(env[cfg], "a").close()
    return env


async def run_build_command(cmd: List[str], cwd: str, home: str, log: List[str]) -> None:
    check_build_argv(cmd)
    try:
        returncode, out, _ = await run_exec(
            cmd, cwd=cwd, env=_build_env(home), timeout=BUILD_TIMEOUT_SECONDS, merge_stderr=True,
        )
    except asyncio.TimeoutError:
        raise BuildError(f"Dependency install timed out after {int(BUILD_TIMEOUT_SECONDS)}s")
    lines = out.decode("utf-8", errors="replace").splitlines()
    log.extend(lines[-100:])
    if returncode != 0:
        tail = "\n".join(lines[-15:])
        raise BuildError(f"Dependency install failed (exit {returncode}):\n{CredentialScrubber.scrub_text(tail)}")


# --- Targets --------------------------------------------------------------

def content_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _CONTENT_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


def build_static_site(source: ExtractedSource, profile: ProjectProfile, workdir: str) -> BuildResult:
    out_dir = profile["static_output_dir"]
    if out_dir is None:
        raise BuildError("No static site output (index.html) was found")
    prefix = f"{out_dir}/" if out_dir else ""
    files = [f for f in source.files if f.path.startswith(prefix)]
    if len(files) > MAX_STATIC_FILES:
        raise BuildError(f"The site has {len(files)} files (max {MAX_STATIC_FILES})")
    result = BuildResult(kind="static_site")
    site_root = os.path.join(workdir, "site")
    for f in files:
        key = f.path[len(prefix):]
        src = os.path.join(source.root, *f.path.split("/"))
        dst = os.path.join(site_root, *key.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        with open(dst, "rb") as fh:
            md5 = hashlib.md5(fh.read(), usedforsecurity=False).hexdigest()
        result.site_files[key] = {"source": f"site/{key}", "content_type": content_type(key), "md5": md5}
    if "index.html" not in result.site_files:
        raise BuildError("index.html is missing from the site output")
    result.log.append(f"Copied {len(files)} files from {out_dir or 'the project root'} into the site bundle")
    return result


def lambda_runtime(profile: ProjectProfile, warnings: List[str]) -> str:
    version = profile["runtime_version"]
    if profile["runtime"] == "python":
        if version in PYTHON_RUNTIMES:
            return PYTHON_RUNTIMES[version]
        if version:
            warnings.append(f"Python {version} isn't an available Lambda runtime; using {DEFAULT_PYTHON}")
        return PYTHON_RUNTIMES[DEFAULT_PYTHON]
    if version in NODE_RUNTIMES:
        return NODE_RUNTIMES[version]
    if version:
        warnings.append(f"Node {version} isn't an available Lambda runtime; using Node {DEFAULT_NODE}")
    return NODE_RUNTIMES[DEFAULT_NODE]


def _write_zip(src_dir: str, zip_path: str) -> None:
    """Deterministic zip: sorted entries, fixed timestamps and permissions, so
    the same input always gives the same source_code_hash."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames.sort()
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            entries.append((os.path.relpath(full, src_dir).replace(os.sep, "/"), full))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, full in sorted(entries):
            info = zipfile.ZipInfo(arc, date_time=_ZIP_EPOCH)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(full, "rb") as fh:
                zf.writestr(info, fh.read())


async def build_lambda(source: ExtractedSource, profile: ProjectProfile, workdir: str) -> BuildResult:
    if not profile["lambda_handler"]:
        raise BuildError("No Lambda handler was detected")
    result = BuildResult(kind="lambda_zip", handler=profile["lambda_handler"])
    result.runtime = lambda_runtime(profile, result.warnings)
    stage = os.path.join(workdir, ".build", "package")
    home = os.path.join(workdir, ".build", "home")
    os.makedirs(stage)
    os.makedirs(home)

    for f in source.files:
        dst = os.path.join(stage, *f.path.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(source.root, *f.path.split("/")), dst)

    manifest = profile["dependency_manifest"]
    reqs: List[str] = []
    if profile["runtime"] == "python" and manifest == "requirements.txt":
        # Always the full validated file - the analyzer's name list skips
        # option lines, which must fail here rather than be silently dropped.
        with open(os.path.join(source.root, "requirements.txt"), encoding="utf-8", errors="replace") as fh:
            reqs = python_requirements(fh.read())
    elif profile["runtime"] == "python" and manifest == "pyproject.toml":
        with open(os.path.join(source.root, "pyproject.toml"), "rb") as fb:
            declared = (tomllib.load(fb).get("project") or {}).get("dependencies") or []
        reqs = [str(r).strip() for r in declared]
        for r in reqs:
            if not _PY_REQUIREMENT.match(r) or "://" in r or "@" in r:
                raise BuildError(f"Unsupported dependency in pyproject.toml: '{r[:80]}' (registry packages only)")
    if reqs:
        version = result.runtime.replace("python", "")
        cmd = [sys.executable, "-m", "pip", "install", "--only-binary=:all:", "--isolated", "--no-input",
               f"--index-url={PYPI_INDEX}", f"--target={stage}", "--platform=manylinux2014_x86_64",
               "--implementation=cp", f"--python-version={version}", "--no-compile", "--disable-pip-version-check",
               "--no-cache-dir", "--upgrade", *reqs]
        result.log.append(f"pip install (wheels only, {result.runtime}): {len(reqs)} requirement(s)")
        await run_build_command(cmd, cwd=stage, home=home, log=result.log)
    elif profile["runtime"] == "node" and profile["dependencies"]:
        try:
            with open(os.path.join(source.root, "package.json"), encoding="utf-8") as fh:
                package = json.load(fh)
            with open(os.path.join(source.root, "package-lock.json"), encoding="utf-8") as fh:
                lock = json.load(fh)
        except FileNotFoundError:
            raise BuildError("package-lock.json is required to install Node dependencies")
        except json.JSONDecodeError:
            raise BuildError("package.json or package-lock.json is not valid JSON")
        validate_npm_manifest(package, lock)
        npm = shutil.which("npm")
        if not npm:
            raise BuildError("npm is not installed on this TerraAgent worker")
        npm_dir = os.path.join(workdir, ".build", "npm")
        os.makedirs(npm_dir)
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(os.path.join(source.root, name), os.path.join(npm_dir, name))
        result.log.append("npm ci (production dependencies, install scripts disabled)")
        await run_build_command(
            [npm, "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund", f"--registry={NPM_REGISTRY}"],
            cwd=npm_dir, home=home, log=result.log,
        )
        shutil.copytree(os.path.join(npm_dir, "node_modules"), os.path.join(stage, "node_modules"), dirs_exist_ok=True)

    os.makedirs(os.path.join(workdir, "artifacts"), exist_ok=True)
    zip_path = os.path.join(workdir, "artifacts", "function.zip")
    await asyncio.to_thread(_write_zip, stage, zip_path)
    size = os.path.getsize(zip_path)
    if size > MAX_LAMBDA_ZIP_BYTES:
        raise BuildError(f"The function package is {size // (1024 * 1024)} MB; Lambda's direct upload limit is 50 MB")
    with open(zip_path, "rb") as fh:
        result.package_sha256_b64 = base64.b64encode(hashlib.sha256(fh.read()).digest()).decode("ascii")
    result.package_file = "artifacts/function.zip"
    result.package_bytes = size
    shutil.rmtree(os.path.join(workdir, ".build"), ignore_errors=True)
    result.log.append(f"Packaged function.zip ({size} bytes)")
    return result


def _stage_source(source: ExtractedSource, stage: str) -> None:
    for f in source.files:
        dst = os.path.join(stage, *f.path.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(source.root, *f.path.split("/")), dst)


def _add_container_recipe(stage: str, app_dir: str, profile: ProjectProfile, port: int, with_db: bool,
                          result: BuildResult, migrate: Optional[List[str]] = None) -> None:
    """Writes a vetted Dockerfile (+ DATABASE_URL entrypoint) into `app_dir`
    of the staged source when the project has no Dockerfile of its own."""
    if profile.get("has_dockerfile"):
        result.log.append(f"Using the project's own Dockerfile in {app_dir or 'the project root'}")
        if with_db:
            result.warnings.append(
                "The project has its own Dockerfile, so DATABASE_URL isn't assembled for it: the container gets "
                "DB_HOST, DB_PORT, DB_NAME, DB_USER and DB_PASSWORD instead."
            )
        return
    app_root = os.path.join(stage, *app_dir.split("/")) if app_dir else stage
    paths = sorted(
        os.path.relpath(os.path.join(d, n), app_root).replace(os.sep, "/")
        for d, _, names in os.walk(app_root) for n in names
    )
    try:
        recipe = generate_container(app_root, paths, profile, port, with_db=with_db, migrate=migrate)
    except DockerfileError as e:
        raise BuildError(str(e)) from e
    for rel, content in recipe.files.items():
        with open(os.path.join(app_root, rel), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        result.generated_files.append(f"{app_dir}/{rel}" if app_dir else rel)
    result.start_command = recipe.command
    result.warnings.extend(recipe.warnings)
    result.log.append(f"No Dockerfile found: generated one from TerraAgent's {profile['runtime']} template "
                      f"(start command: {' '.join(recipe.command)})")


async def _package_source(stage: str, workdir: str, result: BuildResult) -> None:
    os.makedirs(os.path.join(workdir, "artifacts"), exist_ok=True)
    zip_path = os.path.join(workdir, "artifacts", "source.zip")
    await asyncio.to_thread(_write_zip, stage, zip_path)
    size = os.path.getsize(zip_path)
    with open(zip_path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).digest()
    result.package_sha256_b64 = base64.b64encode(digest).decode("ascii")
    result.image_tag = "src-" + digest.hex()[:16]
    result.package_file = "artifacts/source.zip"
    result.package_bytes = size


async def build_container(source: ExtractedSource, profile: ProjectProfile, workdir: str) -> BuildResult:
    """Packages the source tree into a source zip for CodeBuild container deployment,
    adding a vetted Dockerfile if the project has none. Docker builds run exclusively
    inside the customer's AWS account via CodeBuild.
    """
    port = profile.get("listens_on_port") or 8080
    result = BuildResult(kind="container_source", container_port=port)
    stage = os.path.join(workdir, ".build", "source")
    os.makedirs(stage, exist_ok=True)
    _stage_source(source, stage)
    _add_container_recipe(stage, "", profile, port, with_db=False, result=result)
    await _package_source(stage, workdir, result)
    shutil.rmtree(os.path.join(workdir, ".build"), ignore_errors=True)
    result.log.append(f"Packaged source.zip ({result.package_bytes} bytes) for CodeBuild container build (port {port})")
    return result


async def build_fullstack(source: ExtractedSource, profile: ProjectProfile, workdir: str,
                          database_mode: str = "rds") -> BuildResult:
    """Packages the whole project for CodeBuild: the backend image is built from
    its folder (with a vetted Dockerfile if needed) and the frontend, if any, is
    built and uploaded to S3 by the same pipeline - all inside the customer's
    account. No project code runs here."""
    layout = profile.get("fullstack")
    if not layout:
        raise BuildError("No server was found in this project, so it can't be deployed as a full-stack app.")
    backend = layout["backend"]
    db = layout.get("database")
    with_db = bool(database_mode in ("rds", "aurora") and db and db.get("rds_supported"))
    result = BuildResult(kind="fullstack_source", container_port=backend["port"])
    stage = os.path.join(workdir, ".build", "source")
    os.makedirs(stage, exist_ok=True)
    _stage_source(source, stage)
    migration = layout.get("migration") if with_db else None
    _add_container_recipe(stage, backend["dir"], backend["profile"], backend["port"], with_db=with_db, result=result,
                          migrate=(migration or {}).get("command"))
    result.warnings.extend(layout.get("warnings") or [])
    await _package_source(stage, workdir, result)
    shutil.rmtree(os.path.join(workdir, ".build"), ignore_errors=True)
    fe = layout.get("frontend")
    result.log.append(
        f"Packaged source.zip ({result.package_bytes} bytes): backend in {backend['dir'] or 'the project root'}"
        + (f", frontend in {fe['dir'] or 'the project root'}" if fe else ", no separate frontend")
    )
    return result


async def build(source: ExtractedSource, profile: ProjectProfile, target: str, workdir: str,
                settings: Optional[Dict[str, Any]] = None) -> BuildResult:
    if target == "static_site":
        return await asyncio.to_thread(build_static_site, source, profile, workdir)
    if target == "lambda_http":
        return await build_lambda(source, profile, workdir)
    if target == "ecs_service":
        return await build_container(source, profile, workdir)
    if target == "fullstack_app":
        return await build_fullstack(source, profile, workdir, database_mode=(settings or {}).get("database", "rds"))
    raise BuildError(f"Unknown target '{target}'")
