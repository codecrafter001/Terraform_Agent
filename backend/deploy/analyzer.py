"""Project analyzer (design doc §5.1): file tree -> ProjectProfile.

Deterministic and read-only: manifests are parsed with json/tomllib, source
files are matched with anchored regexes, and nothing from the project is ever
imported or executed. Every detected field records the file and rule it came
from, so the UI can show why a target was recommended.
"""

import json
import os
import re
import tomllib
from typing import Any, Dict, List, Optional, Set, TypedDict

from deploy.config import MAX_ANALYZED_SOURCE_FILES

_MAX_READ = 1024 * 1024

STATIC_OUTPUT_DIRS = ("dist", "build", "out", "_site", "site", "www")

NODE_FRAMEWORKS = {
    "next": "next", "nuxt": "nuxt", "@sveltejs/kit": "sveltekit", "@angular/core": "angular",
    "react-scripts": "create-react-app", "vite": "vite", "vue": "vue", "svelte": "svelte", "react": "react",
    "@nestjs/core": "nest", "express": "express", "fastify": "fastify", "koa": "koa", "@hapi/hapi": "hapi",
}
NODE_SERVER_FRAMEWORKS = {"express", "fastify", "koa", "hapi", "nest"}
# Need install scripts / a compiler; `npm ci --ignore-scripts` can't produce them.
NODE_NATIVE_PACKAGES = {"bcrypt", "sharp", "canvas", "sqlite3", "better-sqlite3", "node-sass", "argon2", "re2", "node-gyp"}

PYTHON_FRAMEWORKS = {
    "fastapi": "fastapi", "flask": "flask", "django": "django", "starlette": "starlette",
    "aiohttp": "aiohttp", "bottle": "bottle", "tornado": "tornado",
}
# Source-only distributions: a wheels-only install can't satisfy them.
PYTHON_SOURCE_ONLY_PACKAGES = {"psycopg2", "mysqlclient", "pycrypto", "python-ldap"}

_NODE_HANDLER = re.compile(
    r"(?m)^\s*(?:exports\.handler\s*=|module\.exports\.handler\s*=|export\s+(?:const|let|async\s+function|function)\s+handler\b)"
)
_NODE_LISTEN = re.compile(r"(?:\.listen\(|\bcreateServer\(|\bapp\.listen\()")
_PY_HANDLER = re.compile(r"(?m)^(?:async\s+)?def\s+(lambda_handler|handler)\s*\(\s*event\s*,\s*context\s*\)")
_PY_MANGUM = re.compile(r"(?m)^(\w+)\s*=\s*Mangum\(")
_PY_SERVER = re.compile(r"(?m)(?:\bapp\.run\(|\buvicorn\.run\(|\bweb\.run_app\(|manage\.py)")
_PY_PORT = re.compile(r"\bport\s*=\s*(\d{2,5})")
_DOCKER_EXPOSE = re.compile(r"(?mi)^\s*EXPOSE\s+(\d{2,5})")
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


class Evidence(TypedDict):
    file: str
    rule: str


class ProjectProfile(TypedDict):
    runtime: str  # python | node | static | container | unknown
    runtime_version: Optional[str]
    framework: Optional[str]
    lambda_handler: Optional[str]
    server_entrypoint: bool
    listens_on_port: Optional[int]
    build_required: bool
    static_output_dir: Optional[str]  # "" = the project root
    dependency_manifest: Optional[str]  # requirements.txt | pyproject.toml | package.json | unsupported:<file>
    dependencies: List[str]
    has_lockfile: bool
    native_dependencies: List[str]
    source_bytes: int
    has_dockerfile: bool
    evidence: Dict[str, List[Evidence]]
    warnings: List[str]
    fullstack: Optional[Dict[str, Any]]  # deploy.fullstack.FullstackLayout, filled in by the pipeline


def _obj(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _norm_pkg(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class _Tree:
    def __init__(self, root: str, paths: List[str], sizes: Dict[str, int]):
        self.root = root
        self.paths = set(paths)
        self.sizes = sizes

    def has(self, path: str) -> bool:
        return path in self.paths

    def read(self, path: str) -> Optional[str]:
        if path not in self.paths or self.sizes.get(path, 0) > _MAX_READ:
            return None
        with open(os.path.join(self.root, *path.split("/")), "rb") as f:
            raw = f.read()
        if b"\x00" in raw[:8192]:
            return None
        return raw.decode("utf-8", errors="replace")

    def with_suffix(self, *suffixes: str, max_depth: int = 3) -> List[str]:
        found = sorted(p for p in self.paths if p.endswith(suffixes) and p.count("/") < max_depth)
        return found[:MAX_ANALYZED_SOURCE_FILES]


def _python_requirements(text: str) -> List[str]:
    names = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _REQ_NAME.match(line)
        if m:
            names.append(m.group(1))
    return names


def _python_version(tree: _Tree, pyproject: Dict[str, Any], ev: List[Evidence]) -> Optional[str]:
    text = tree.read(".python-version")
    if text and (m := re.match(r"^\s*(3\.\d+)", text)):
        ev.append({"file": ".python-version", "rule": "python.version_file"})
        return m.group(1)
    text = tree.read("runtime.txt")
    if text and (m := re.search(r"python-(3\.\d+)", text)):
        ev.append({"file": "runtime.txt", "rule": "python.runtime_txt"})
        return m.group(1)
    requires = str((pyproject.get("project") or {}).get("requires-python") or "")
    if m := re.search(r"(3\.\d+)", requires):
        ev.append({"file": "pyproject.toml", "rule": "python.requires_python"})
        return m.group(1)
    return None


def _node_version(pkg: Dict[str, Any], ev: List[Evidence]) -> Optional[str]:
    engines = _obj(pkg.get("engines"))
    spec = str(engines.get("node") or "")
    if m := re.search(r"(\d{2})", spec):
        ev.append({"file": "package.json", "rule": "node.engines"})
        return m.group(1)
    return None


def analyze(root: str, paths: List[str], sizes: Optional[Dict[str, int]] = None) -> ProjectProfile:
    tree = _Tree(root, paths, sizes or {})
    evidence: Dict[str, List[Evidence]] = {}
    warnings: List[str] = []

    def ev(field: str) -> List[Evidence]:
        return evidence.setdefault(field, [])

    profile: ProjectProfile = {
        "runtime": "unknown", "runtime_version": None, "framework": None, "lambda_handler": None,
        "server_entrypoint": False, "listens_on_port": None, "build_required": False,
        "static_output_dir": None, "dependency_manifest": None, "dependencies": [], "has_lockfile": False,
        "native_dependencies": [], "source_bytes": sum((sizes or {}).values()),
        "has_dockerfile": tree.has("Dockerfile"), "evidence": evidence, "warnings": warnings,
        "fullstack": None,
    }
    if profile["has_dockerfile"]:
        ev("has_dockerfile").append({"file": "Dockerfile", "rule": "container.dockerfile"})
        docker = tree.read("Dockerfile") or ""
        if m := _DOCKER_EXPOSE.search(docker):
            profile["listens_on_port"] = int(m.group(1))
            ev("listens_on_port").append({"file": "Dockerfile", "rule": "container.expose"})

    # --- Node -------------------------------------------------------------
    pkg: Dict[str, Any] = {}
    has_pkg = tree.has("package.json")
    if tree.has("package.json"):
        try:
            loaded = json.loads(tree.read("package.json") or "{}")
            pkg = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            warnings.append("package.json is not valid JSON")
    has_build_script = bool(isinstance(pkg.get("scripts"), dict) and pkg["scripts"].get("build"))
    node_handler: Optional[str] = None
    node_server = False
    if has_pkg:
        deps = _obj(pkg.get("dependencies"))
        dev = _obj(pkg.get("devDependencies"))
        all_deps = {**dev, **deps}
        # Prioritize server frameworks (e.g. express, fastify) so full-stack apps get recognized as container/server
        server_fw = next((name for dep, name in NODE_FRAMEWORKS.items() if dep in all_deps and name in NODE_SERVER_FRAMEWORKS), None)
        client_fw = next((name for dep, name in NODE_FRAMEWORKS.items() if dep in all_deps and name not in NODE_SERVER_FRAMEWORKS), None)
        chosen_fw = server_fw or client_fw
        if chosen_fw:
            profile["framework"] = chosen_fw
            ev("framework").append({"file": "package.json", "rule": f"node.dep.{chosen_fw}"})
        profile["native_dependencies"] = sorted(d for d in deps if d in NODE_NATIVE_PACKAGES)
        profile["has_lockfile"] = tree.has("package-lock.json")
        candidates = [str(pkg["main"])] if isinstance(pkg.get("main"), str) else []
        if isinstance(pkg.get("scripts"), dict):
            for s in ("start", "dev", "serve"):
                val = str(pkg["scripts"].get(s, ""))
                for token in val.split():
                    clean = token.lstrip("./").strip("'\"")
                    if clean.endswith((".js", ".mjs", ".ts", ".tsx")):
                        candidates.append(clean)
        candidates += [
            "index.js", "index.mjs", "index.ts", "handler.js", "handler.mjs", "handler.ts",
            "lambda.js", "lambda.ts", "app.js", "app.ts", "server.js", "server.ts",
            "server/index.ts", "server/index.js", "src/server.ts", "src/server.js",
            "src/index.js", "src/index.ts", "src/handler.js", "src/handler.ts", "src/app.ts", "src/app.js",
        ]
        seen: Set[str] = set()
        for path in candidates:
            path = path.lstrip("./")
            if path in seen:  # the same file is often named by main, start and dev
                continue
            seen.add(path)
            text = tree.read(path)
            if text is None:
                continue
            if node_handler is None and _NODE_HANDLER.search(text):
                node_handler = f"{path.rsplit('.', 1)[0]}.handler"
                ev("lambda_handler").append({"file": path, "rule": "node.exports_handler"})
            if m := _NODE_LISTEN.search(text):
                node_server = True
                ev("server_entrypoint").append({"file": path, "rule": "node.listen"})
                if m_port := re.search(r"\.listen\(\s*(?:process\.env\.PORT\s*\|\|\s*)?(\d{2,5})", text):
                    profile["listens_on_port"] = int(m_port.group(1))
                elif port_m := re.search(r'process\.env\.PORT\s*\|\|\s*["\']?(\d{2,5})["\']?', text):
                    profile["listens_on_port"] = int(port_m.group(1))
        if node_server and profile["listens_on_port"] is None:
            profile["listens_on_port"] = 8080
        if node_handler or node_server or profile["framework"] in NODE_SERVER_FRAMEWORKS:
            profile["dependency_manifest"] = "package.json"
            profile["dependencies"] = sorted(deps)
        profile["runtime_version"] = _node_version(pkg, ev("runtime_version"))

    # --- Python -----------------------------------------------------------
    pyproject: Dict[str, Any] = {}
    if tree.has("pyproject.toml"):
        try:
            pyproject = tomllib.loads(tree.read("pyproject.toml") or "")
        except tomllib.TOMLDecodeError:
            warnings.append("pyproject.toml is not valid TOML")
    py_deps: List[str] = []
    py_manifest: Optional[str] = None
    if tree.has("requirements.txt"):
        py_manifest = "requirements.txt"
        py_deps = _python_requirements(tree.read("requirements.txt") or "")
    elif isinstance((pyproject.get("project") or {}).get("dependencies"), list):
        py_manifest = "pyproject.toml"
        py_deps = [m.group(1) for d in pyproject["project"]["dependencies"] if (m := _REQ_NAME.match(str(d)))]
    elif tree.has("Pipfile") or (pyproject.get("tool") or {}).get("poetry"):
        py_manifest = "unsupported:" + ("Pipfile" if tree.has("Pipfile") else "pyproject.toml (Poetry)")

    py_files = tree.with_suffix(".py")
    py_handler: Optional[str] = None
    py_server = False
    for path in py_files:
        text = tree.read(path)
        if text is None:
            continue
        module = path[:-3].replace("/", ".")
        if py_handler is None:
            if m := _PY_HANDLER.search(text):
                py_handler = f"{module}.{m.group(1)}"
                ev("lambda_handler").append({"file": path, "rule": "python.def_handler"})
            elif m := _PY_MANGUM.search(text):
                py_handler = f"{module}.{m.group(1)}"
                ev("lambda_handler").append({"file": path, "rule": "python.mangum_adapter"})
        if _PY_SERVER.search(text):
            py_server = True
            ev("server_entrypoint").append({"file": path, "rule": "python.server_run"})
            if m := _PY_PORT.search(text):
                profile["listens_on_port"] = int(m.group(1))
    if tree.has("manage.py"):
        py_server = True
        ev("server_entrypoint").append({"file": "manage.py", "rule": "python.django_manage"})

    is_python = bool(py_manifest or py_files)
    is_node = has_pkg

    # --- Runtime ----------------------------------------------------------
    if py_handler or (is_python and not node_handler and not is_node):
        profile["runtime"] = "python"
        profile["lambda_handler"] = py_handler
        profile["server_entrypoint"] = py_server
        profile["dependency_manifest"] = py_manifest
        profile["dependencies"] = sorted(py_deps)
        profile["native_dependencies"] = sorted(d for d in py_deps if _norm_pkg(d) in PYTHON_SOURCE_ONLY_PACKAGES)
        profile["has_lockfile"] = False
        for dep in py_deps:
            if (fw := PYTHON_FRAMEWORKS.get(_norm_pkg(dep))):
                profile["framework"] = fw
                ev("framework").append({"file": py_manifest or "", "rule": f"python.dep.{_norm_pkg(dep)}"})
                break
        profile["runtime_version"] = _python_version(tree, pyproject, ev("runtime_version"))
    elif is_node:
        profile["runtime"] = "node"
        profile["lambda_handler"] = node_handler
        profile["server_entrypoint"] = node_server or profile["framework"] in NODE_SERVER_FRAMEWORKS
    if not profile["lambda_handler"]:
        evidence.pop("lambda_handler", None)

    # --- Static output ----------------------------------------------------
    is_server = bool(profile["lambda_handler"] or profile["server_entrypoint"])
    if not is_server:
        # With a build script, a root or public/ index.html is the source
        # template (vite, create-react-app); only committed build output counts.
        # Without one, the root index.html is the site.
        static_candidates = [(d, "static.output_dir") for d in STATIC_OUTPUT_DIRS]
        if not has_build_script:
            static_candidates = [("", "static.root_index"), *static_candidates, ("public", "static.public_dir")]
        for d, rule in static_candidates:
            index = f"{d}/index.html" if d else "index.html"
            if tree.has(index):
                profile["static_output_dir"] = d
                ev("static_output_dir").append({"file": index, "rule": rule})
                break
        if profile["runtime"] == "unknown" and profile["static_output_dir"] is not None:
            profile["runtime"] = "static"
        if has_build_script and profile["static_output_dir"] is None:
            profile["build_required"] = True
            ev("build_required").append({"file": "package.json", "rule": "node.build_script_without_output"})

    if profile["runtime"] == "unknown" and profile["has_dockerfile"]:
        profile["runtime"] = "container"
    return profile
