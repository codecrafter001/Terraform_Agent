"""Full-stack layout detection (design doc §5.1, fullstack_app target).

Finds, deterministically and without running anything:
- the backend: a server in the project root or in backend/, server/, api/
- an optional separately-built frontend: a client framework with a build
  script (or committed build output) in frontend/, client/, web/, ui/, webapp/
- the database the backend talks to, from its dependencies
- the environment variable *names* the backend reads (never values; .env
  files never even reach the analyzer)

Each sub-project is analysed with the same analyzer as a single-app upload.
"""

import json
import re
from typing import Dict, List, Optional, Tuple, TypedDict, cast

from deploy.analyzer import Evidence, ProjectProfile, _Tree, analyze

FRONTEND_DIRS = ("frontend", "client", "web", "ui", "webapp")
BACKEND_DIRS = ("backend", "server", "api")
CLIENT_FRAMEWORKS = {"react", "vite", "vue", "svelte", "angular", "create-react-app", "sveltekit", "next"}
SSR_ONLY_FRAMEWORKS = {"nuxt"}

MAX_ENV_KEYS = 25
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_NODE_ENV_REF = re.compile(r"process\.env(?:\.([A-Z][A-Z0-9_]*)|\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]\s*\])")
_PY_ENV_REF = re.compile(
    r"os\.(?:environ\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]\s*\]|environ\.get\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"
    r"|getenv\(\s*['\"]([A-Z][A-Z0-9_]*)['\"])"
)
_FRONT_ENV_REF = re.compile(r"(?:import\.meta\.env\.|process\.env\.)((?:VITE|REACT_APP|NEXT_PUBLIC|VUE_APP)_[A-Z0-9_]+)")
_API_ROUTE = re.compile(r"['\"`]/api(?:/|['\"`])")
_LOCALHOST_URL = re.compile(r"https?://(?:localhost|127\.0\.0\.1):\d+")
_PRISMA_PROVIDER = re.compile(r"(?s)datasource\s+\w+\s*\{.*?provider\s*=\s*\"(\w+)\"")
_NEXT_EXPORT = re.compile(r"output\s*:\s*['\"]export['\"]")

# Set by TerraAgent (or meaningless in a container) - never asked for as secrets.
PROVIDED_ENV = {
    "PORT", "HOST", "NODE_ENV", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "TZ", "HOME", "PATH", "PWD",
    "CI", "DEBUG", "LOG_LEVEL", "HOSTNAME",
}
DB_ENV = {
    "DATABASE_URL", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_URL_SCHEME",
    "PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE",
    "MYSQL_USER", "MYSQL_PASSWORD", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER",
    "POSTGRES_PASSWORD",
}

NODE_DB_DRIVERS: Dict[str, str] = {
    "pg": "postgres", "postgres": "postgres", "pg-promise": "postgres", "@vercel/postgres": "postgres",
    "mysql": "mysql", "mysql2": "mysql", "mariadb": "mysql",
    "mongoose": "mongodb", "mongodb": "mongodb",
    "sqlite3": "sqlite", "better-sqlite3": "sqlite",
}
PY_DB_DRIVERS: Dict[str, Tuple[str, str]] = {
    "psycopg2": ("postgres", "postgresql"), "psycopg2-binary": ("postgres", "postgresql"),
    "psycopg": ("postgres", "postgresql+psycopg"), "psycopg-binary": ("postgres", "postgresql+psycopg"),
    "asyncpg": ("postgres", "postgresql+asyncpg"),
    "pymysql": ("mysql", "mysql+pymysql"), "mysqlclient": ("mysql", "mysql"), "aiomysql": ("mysql", "mysql+aiomysql"),
    "mysql-connector-python": ("mysql", "mysql+mysqlconnector"),
    "pymongo": ("mongodb", ""), "motor": ("mongodb", ""), "mongoengine": ("mongodb", ""),
}
SCHEMES = {"postgres": "postgresql", "mysql": "mysql"}


class DatabaseInfo(TypedDict):
    engine: str  # postgres | mysql | mongodb | sqlite
    url_scheme: Optional[str]  # scheme the generated entrypoint puts in DATABASE_URL
    rds_supported: bool
    evidence: List[Evidence]
    warnings: List[str]


class FrontendInfo(TypedDict):
    dir: str  # relative to the project root
    framework: Optional[str]
    build_required: bool  # built in CodeBuild (npm run build) vs uploaded as committed
    static_output_dir: Optional[str]  # committed output, relative to dir ("" = dir itself)
    has_lockfile: bool
    api_url_env: List[str]  # build-time variables pointed at the deployed API
    appends_api_prefix: bool
    evidence: List[Evidence]


class BackendInfo(TypedDict):
    dir: str  # "" = project root
    runtime: str
    framework: Optional[str]
    port: int
    has_dockerfile: bool
    uses_api_prefix: bool
    profile: ProjectProfile


class MigrationInfo(TypedDict):
    command: List[str]  # one of the fixed forms in _migration(), run by the generated entrypoint
    evidence: List[Evidence]


class AddonInfo(TypedDict):
    evidence: List[Evidence]


class WorkerInfo(TypedDict):
    command: List[str]  # one of the fixed forms in _worker(), run in a second ECS service
    evidence: List[Evidence]


class FullstackLayout(TypedDict):
    backend: BackendInfo
    frontend: Optional[FrontendInfo]
    database: Optional[DatabaseInfo]
    migration: Optional[MigrationInfo]
    cache: Optional[AddonInfo]  # the backend talks to Redis/Valkey
    object_storage: Optional[AddonInfo]  # the backend stores files in S3
    worker: Optional[WorkerInfo]  # a background worker process
    env_keys: List[str]
    warnings: List[str]


NODE_CACHE_DEPS = ("redis", "ioredis", "bull", "bullmq", "@redis/client", "connect-redis", "rate-limit-redis")
PY_CACHE_DEPS = ("redis", "django-redis", "rq", "celery", "flask-caching", "aioredis")
NODE_S3_DEPS = ("multer-s3", "@aws-sdk/client-s3", "@aws-sdk/lib-storage", "aws-sdk", "s3-upload-stream")
PY_S3_DEPS = ("boto3", "django-storages", "aioboto3", "s3fs")
WORKER_SCRIPTS = ("worker", "start:worker", "worker:start", "queue", "jobs")
_PY_CELERY_APP = re.compile(r"(?m)^(\w+)\s*=\s*Celery\(")


# package.json scripts that apply a schema, most specific first.
MIGRATION_SCRIPTS = ("db:migrate", "migrate", "db:deploy", "prisma:migrate", "db:push")
_SCRIPT_NAME = re.compile(r"^[A-Za-z0-9:_-]{1,40}$")
# Platform-only variables that must never become required secrets.
_PLATFORM_ENV_PREFIXES = ("REPL_", "REPLIT_", "VERCEL_", "NETLIFY_", "RENDER_", "RAILWAY_", "HEROKU_")


def _sub(paths: List[str], sizes: Dict[str, int], prefix: str) -> Tuple[List[str], Dict[str, int]]:
    if not prefix:
        return list(paths), dict(sizes)
    start = prefix + "/"
    sub = [p[len(start):] for p in paths if p.startswith(start)]
    return sub, {p[len(start):]: s for p, s in sizes.items() if p.startswith(start)}


def _source_texts(tree: _Tree, suffixes: Tuple[str, ...], max_depth: int = 6) -> List[Tuple[str, str]]:
    out = []
    for path in tree.with_suffix(*suffixes, max_depth=max_depth):
        text = tree.read(path)
        if text is not None:
            out.append((path, text))
    return out


def _is_server(p: ProjectProfile) -> bool:
    return p["runtime"] in ("node", "python") and (p["server_entrypoint"] or p["has_dockerfile"]) and not p["lambda_handler"]


def _frontend(root: str, paths: List[str], sizes: Dict[str, int], prefix: str,
              root_builds: bool = False) -> Optional[FrontendInfo]:
    sub_paths, sub_sizes = _sub(paths, sizes, prefix)
    if not sub_paths:
        return None
    base = f"{root}/{prefix}" if prefix else root
    tree = _Tree(base, sub_paths, sub_sizes)
    # A sub-folder without its own package.json in a project whose root has a build
    # script (Vite/Replit-style: `vite build` with root: "client") is the source of
    # the root build, served by the backend - not a separate frontend.
    if prefix and root_builds and not tree.has("package.json"):
        return None
    ev: List[Evidence] = []
    pkg: Dict = {}
    if tree.has("package.json"):
        try:
            loaded = json.loads(tree.read("package.json") or "{}")
            pkg = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            pkg = {}
    p = analyze(base, sub_paths, sub_sizes)
    def rel(f):
        return f"{prefix}/{f}" if prefix else f
    framework = p["framework"]
    has_build = bool(isinstance(pkg.get("scripts"), dict) and pkg["scripts"].get("build"))
    if _is_server(p) and framework not in CLIENT_FRAMEWORKS:
        return None
    if framework in SSR_ONLY_FRAMEWORKS:
        return None
    if framework == "next":
        config = next((tree.read(c) for c in ("next.config.js", "next.config.mjs", "next.config.ts") if tree.has(c)), "")
        if not _NEXT_EXPORT.search(config or ""):
            return None  # SSR Next.js needs a server, not S3
        ev.append({"file": rel("next.config.js"), "rule": "frontend.next_static_export"})
    if has_build and framework in CLIENT_FRAMEWORKS:
        ev.append({"file": rel("package.json"), "rule": f"frontend.build.{framework}"})
        build_required, output = True, None
    elif p["static_output_dir"] is not None:
        ev.append({"file": rel((p["static_output_dir"] + "/" if p["static_output_dir"] else "") + "index.html"),
                   "rule": "frontend.static_output"})
        build_required, output = False, p["static_output_dir"]
    else:
        return None

    texts = _source_texts(tree, (".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".mjs"))
    api_vars: List[str] = []
    appends = False
    for _, text in texts:
        for name in _FRONT_ENV_REF.findall(text):
            if any(k in name for k in ("API", "BACKEND", "SERVER")) and name not in api_vars:
                api_vars.append(name)
        for name in api_vars:
            if re.search(re.escape(name) + r"[^\n]{0,20}/api\b", text):
                appends = True
    return {
        "dir": prefix, "framework": framework, "build_required": build_required, "static_output_dir": output,
        "has_lockfile": tree.has("package-lock.json"), "api_url_env": sorted(api_vars)[:10],
        "appends_api_prefix": appends, "evidence": ev,
    }


def _database(root: str, backend: BackendInfo, paths: List[str]) -> Optional[DatabaseInfo]:
    p = backend["profile"]
    deps = {d.lower() for d in p["dependencies"]}
    prefix = backend["dir"]
    def rel(f):
        return f"{prefix}/{f}" if prefix else f
    manifest = rel(p["dependency_manifest"] or "package.json")
    found: Optional[str] = None
    scheme: Optional[str] = None
    ev: List[Evidence] = []
    warnings: List[str] = []

    if p["runtime"] == "node":
        schema_path = next((c for c in (rel("prisma/schema.prisma"), "prisma/schema.prisma") if c in paths), None)
        if schema_path:
            tree = _Tree(root, paths, {})
            if m := _PRISMA_PROVIDER.search(tree.read(schema_path) or ""):
                provider = m.group(1)
                found = {"postgresql": "postgres", "postgres": "postgres", "mysql": "mysql",
                         "mongodb": "mongodb", "sqlite": "sqlite"}.get(provider)
                if found:
                    ev.append({"file": schema_path, "rule": f"db.prisma.{provider}"})
        if not found:
            for dep, engine in NODE_DB_DRIVERS.items():
                if dep in deps:
                    found = engine
                    ev.append({"file": manifest, "rule": f"db.node.{dep}"})
                    break
        if not found and "@neondatabase/serverless" in deps:
            found = "postgres"
            ev.append({"file": manifest, "rule": "db.node.neon_serverless"})
            warnings.append(
                "The backend uses Neon's serverless driver (@neondatabase/serverless), which connects over "
                "WebSockets and can't talk to Amazon RDS. Switch to the 'pg' driver to use RDS, or choose "
                "'external database' and paste your Neon URL into the DATABASE_URL secret."
            )
        if found in SCHEMES:
            scheme = SCHEMES[found]
    else:
        for dep, (engine, s) in PY_DB_DRIVERS.items():
            if dep in deps:
                found, scheme = engine, s or None
                ev.append({"file": manifest, "rule": f"db.python.{dep}"})
                break
        if not found and "django" in deps:
            tree = _Tree(root, paths, {})
            for path in (x for x in paths if x.endswith("settings.py")):
                text = tree.read(path) or ""
                if "django.db.backends.postgresql" in text:
                    found, scheme = "postgres", "postgresql"
                elif "django.db.backends.mysql" in text:
                    found, scheme = "mysql", "mysql"
                if found:
                    ev.append({"file": path, "rule": f"db.django.{found}"})
                    warnings.append(
                        "Django settings must read the database from environment variables (DB_HOST, DB_NAME, "
                        "DB_USER, DB_PASSWORD, DB_PORT, or DATABASE_URL via dj-database-url)."
                    )
                    break
    if not found:
        return None
    if found == "mongodb":
        warnings.append(
            "MongoDB isn't provisioned by TerraAgent (Amazon DocumentDB isn't fully compatible). Use MongoDB "
            "Atlas or another host and paste its connection string into the generated secret."
        )
    if found == "sqlite":
        warnings.append(
            "SQLite stores data inside the container, which is lost on every restart or redeploy. "
            "Switch to PostgreSQL or MySQL to keep data."
        )
    return {"engine": found, "url_scheme": scheme, "rds_supported": found in SCHEMES, "evidence": ev, "warnings": warnings}


def _env_keys(tree: _Tree, runtime: str, warnings: List[str]) -> List[str]:
    keys: List[str] = []
    aws_keys: List[str] = []
    suffixes = (".py",) if runtime == "python" else (".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx")
    pattern = _PY_ENV_REF if runtime == "python" else _NODE_ENV_REF
    for _, text in _source_texts(tree, suffixes):
        for groups in pattern.findall(text):
            name = next((g for g in (groups if isinstance(groups, tuple) else (groups,)) if g), "")
            if not _ENV_KEY.match(name) or name in PROVIDED_ENV or name in DB_ENV or name in keys:
                continue
            if name.startswith("AWS_"):
                if name not in aws_keys:
                    aws_keys.append(name)
                continue
            if name.startswith(("VITE_", "REACT_APP_", "NEXT_PUBLIC_", "VUE_APP_", "npm_", *_PLATFORM_ENV_PREFIXES)):
                continue
            keys.append(name)
    if aws_keys:
        warnings.append(
            f"The backend reads {', '.join(sorted(aws_keys)[:5])}. AWS keys are never stored as secrets: the "
            "container gets AWS access through its own IAM task role instead."
        )
    keys.sort()
    if len(keys) > MAX_ENV_KEYS:
        warnings.append(f"Only the first {MAX_ENV_KEYS} environment variables get a secret; add the rest manually.")
    return keys[:MAX_ENV_KEYS]


def detect(root: str, paths: List[str], sizes: Dict[str, int], root_profile: ProjectProfile) -> Optional[FullstackLayout]:
    """The project's full-stack layout, or None when no server was found."""
    warnings: List[str] = []
    backend: Optional[BackendInfo] = None
    # A copy: the pipeline stores this layout inside root_profile itself, so keeping
    # the same object here would make the saved profile a circular structure.
    candidates: List[Tuple[str, ProjectProfile]] = []
    if _is_server(root_profile):
        candidates.append(("", cast(ProjectProfile, {**root_profile, "fullstack": None})))
    for d in BACKEND_DIRS:
        sub_paths, sub_sizes = _sub(paths, sizes, d)
        if sub_paths:
            candidates.append((d, analyze(f"{root}/{d}", sub_paths, sub_sizes)))
    for d, p in candidates:
        if _is_server(p):
            tree = _Tree(f"{root}/{d}" if d else root, *_sub(paths, sizes, d))
            texts = _source_texts(tree, (".py",) if p["runtime"] == "python" else (".js", ".ts", ".mjs", ".cjs"))
            backend = {
                "dir": d, "runtime": p["runtime"], "framework": p["framework"],
                "port": p["listens_on_port"] or 8080, "has_dockerfile": p["has_dockerfile"],
                "uses_api_prefix": any(_API_ROUTE.search(t) for _, t in texts), "profile": p,
            }
            break
    if backend is None:
        return None

    root_builds = _root_has_build_script(root, paths)
    frontend: Optional[FrontendInfo] = None
    for d in FRONTEND_DIRS:
        if d != backend["dir"] and any(x.startswith(d + "/") for x in paths):
            if frontend := _frontend(root, paths, sizes, d, root_builds=root_builds):
                break
    if frontend is None and backend["dir"]:
        frontend = _frontend(root, paths, sizes, "")  # root frontend + backend/ subdir
    if frontend:
        sub_paths, sub_sizes = _sub(paths, sizes, frontend["dir"])
        tree = _Tree(f"{root}/{frontend['dir']}" if frontend["dir"] else root, sub_paths, sub_sizes)
        for path, text in _source_texts(tree, (".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte")):
            if _LOCALHOST_URL.search(text):
                where = f"{frontend['dir']}/{path}" if frontend["dir"] else path
                warnings.append(
                    f"{where} calls a hard-coded localhost URL; it won't work once deployed. Use a relative "
                    "URL (/api/...) or an API URL environment variable."
                )
                break

    database = _database(root, backend, paths)
    tree = _Tree(f"{root}/{backend['dir']}" if backend["dir"] else root, *_sub(paths, sizes, backend["dir"]))
    env_keys = _env_keys(tree, backend["runtime"], warnings)
    if database and database["engine"] == "mongodb":
        for key in ("MONGODB_URI", "MONGO_URI", "MONGO_URL", "MONGODB_URL"):
            if key in env_keys:
                break
        else:
            env_keys = sorted({*env_keys, "MONGODB_URI"})[:MAX_ENV_KEYS]
    if database:
        warnings.extend(database["warnings"])
    migration = _migration(root, backend, paths) if database and database["rds_supported"] else None
    deps = {d.lower() for d in backend["profile"]["dependencies"]}
    manifest = (f"{backend['dir']}/" if backend["dir"] else "") + (backend["profile"]["dependency_manifest"] or "package.json")
    node = backend["runtime"] == "node"
    cache_dep = next((d for d in (NODE_CACHE_DEPS if node else PY_CACHE_DEPS) if d in deps), None)
    s3_dep = next((d for d in (NODE_S3_DEPS if node else PY_S3_DEPS) if d in deps), None)
    layout: FullstackLayout = {
        "backend": backend, "frontend": frontend, "database": database, "migration": migration,
        "cache": {"evidence": [{"file": manifest, "rule": f"cache.dep.{cache_dep}"}]} if cache_dep else None,
        "object_storage": {"evidence": [{"file": manifest, "rule": f"s3.dep.{s3_dep}"}]} if s3_dep else None,
        "worker": _worker(root, backend, paths),
        "env_keys": env_keys, "warnings": warnings,
    }
    return layout


def _worker(root: str, backend: BackendInfo, paths: List[str]) -> Optional[WorkerInfo]:
    """The background worker command, as one of a few fixed forms - never a command
    string taken from the project."""
    prefix = backend["dir"]
    def rel(f):
        return f"{prefix}/{f}" if prefix else f
    tree = _Tree(root, paths, {})
    if backend["runtime"] == "node":
        try:
            pkg = json.loads(tree.read(rel("package.json")) or "{}")
        except json.JSONDecodeError:
            return None
        scripts = pkg.get("scripts") if isinstance(pkg, dict) and isinstance(pkg.get("scripts"), dict) else {}
        for name in WORKER_SCRIPTS:
            if name in scripts and _SCRIPT_NAME.match(name):
                return {"command": ["npm", "run", name], "evidence": [{"file": rel("package.json"), "rule": f"worker.script.{name}"}]}
        return None
    deps = {d.lower() for d in backend["profile"]["dependencies"]}
    if "celery" in deps:
        for path in sorted(p for p in paths if p.endswith(".py") and (not prefix or p.startswith(prefix + "/"))):
            if m := _PY_CELERY_APP.search(tree.read(path) or ""):
                module = (path[len(prefix) + 1:] if prefix else path)[:-3].replace("/", ".")
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,200}", module):
                    return {"command": ["celery", "-A", f"{module}:{m.group(1)}", "worker", "--loglevel=INFO"],
                            "evidence": [{"file": path, "rule": "worker.celery_app"}]}
    if "rq" in deps:
        return {"command": ["rq", "worker"], "evidence": [{"file": rel(backend["profile"]["dependency_manifest"] or "requirements.txt"), "rule": "worker.rq"}]}
    return None


def _root_has_build_script(root: str, paths: List[str]) -> bool:
    if "package.json" not in paths:
        return False
    try:
        pkg = json.loads(_Tree(root, paths, {}).read("package.json") or "{}")
    except json.JSONDecodeError:
        return False
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    return bool(isinstance(scripts, dict) and scripts.get("build"))


def _migration(root: str, backend: BackendInfo, paths: List[str]) -> Optional[MigrationInfo]:
    """The schema command to run before the server starts, as one of a few fixed
    forms - never a command string taken from the project."""
    prefix = backend["dir"]
    def rel(f):
        return f"{prefix}/{f}" if prefix else f
    tree = _Tree(root, paths, {})
    if backend["runtime"] == "node":
        try:
            pkg = json.loads(tree.read(rel("package.json")) or "{}")
        except json.JSONDecodeError:
            pkg = {}
        scripts = pkg.get("scripts") if isinstance(pkg, dict) and isinstance(pkg.get("scripts"), dict) else {}
        for name in MIGRATION_SCRIPTS:
            if name in scripts and _SCRIPT_NAME.match(name):
                return {"command": ["npm", "run", name], "evidence": [{"file": rel("package.json"), "rule": f"migrate.script.{name}"}]}
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})} if isinstance(pkg, dict) else {}
        if "prisma" in deps and rel("prisma/schema.prisma") in paths:
            has_migrations = any(p.startswith(rel("prisma/migrations/")) for p in paths)
            command = ["npx", "prisma", "migrate", "deploy"] if has_migrations else ["npx", "prisma", "db", "push"]
            return {"command": command, "evidence": [{"file": rel("prisma/schema.prisma"), "rule": "migrate.prisma"}]}
        return None
    if rel("alembic.ini") in paths:
        return {"command": ["alembic", "upgrade", "head"], "evidence": [{"file": rel("alembic.ini"), "rule": "migrate.alembic"}]}
    if rel("manage.py") in paths:
        return {"command": ["python", "manage.py", "migrate", "--noinput"],
                "evidence": [{"file": rel("manage.py"), "rule": "migrate.django"}]}
    return None
