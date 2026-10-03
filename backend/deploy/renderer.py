"""Renders a deployment's Terraform project from TerraAgent's own templates
(design doc §6).

The template .tf files are copied verbatim; every user- or project-derived
value goes into terraform.tfvars.json - JSON, not HCL - so no string from an
upload can ever become Terraform syntax. No LLM is involved.
"""

import json
import os
import re
from typing import Any, Dict, Optional

from deploy.builder import BuildResult

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
TFVARS_FILENAME = "terraform.tfvars.json"
TEMPLATE_VERSION = 1

_DEPLOYMENT_ID = re.compile(r"^dep-[0-9a-f]{12}$")


def template_files(target: str) -> Dict[str, str]:
    """name -> content of the target's template .tf files."""
    directory = os.path.join(TEMPLATE_DIR, target)
    if not os.path.isdir(directory) or "/" in target or "\\" in target or target.startswith("."):
        raise ValueError(f"Unknown deployment target '{target}'")
    files: Dict[str, str] = {}
    for name in sorted(os.listdir(directory)):
        if name.endswith(".tf"):
            with open(os.path.join(directory, name), encoding="utf-8") as f:
                files[name] = f.read()
    return files


def _container_vars(settings: Dict[str, Any], build: BuildResult) -> Dict[str, Any]:
    """Variables shared by the ecs_service and fullstack_app templates."""
    if not build.package_file or not build.image_tag:
        raise ValueError("container targets need a packaged source.zip")
    return {
        "source_file": build.package_file,
        "image_tag": str(settings.get("image_tag") or build.image_tag),
        "container_port": int(settings.get("container_port") or build.container_port or 8080),
        "cpu": int(settings.get("cpu", 256)),
        "memory_mb": int(settings.get("memory_mb", 512)),
        "desired_count": int(settings.get("desired_count", 1)),
        "health_check_path": str(settings.get("health_check_path") or "/"),
        "log_retention_days": int(settings.get("log_retention_days", 30)),
        "permissions_boundary_arn": settings.get("permissions_boundary_arn") or None,
    }


def _fullstack_vars(settings: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    layout = profile.get("fullstack") or {}
    backend = layout.get("backend") or {}
    frontend = layout.get("frontend")
    db = layout.get("database")
    mode = settings.get("database") or ("rds" if db and db.get("rds_supported") else "none")
    if mode == "rds" and not (db and db.get("rds_supported")):
        raise ValueError("an RDS database needs a detected PostgreSQL or MySQL driver")
    engine = (db or {}).get("engine") if mode == "rds" else None
    keys = settings.get("secret_env_keys")
    secret_keys = sorted(set((layout.get("env_keys") or []) if keys is None else keys))
    if mode == "external" and "DATABASE_URL" not in secret_keys:
        secret_keys = sorted([*secret_keys, "DATABASE_URL"])
    committed_output = bool(frontend) and not frontend.get("build_required")
    return {
        "backend_dir": backend.get("dir") or "",
        "frontend_enabled": bool(frontend),
        "frontend_dir": (frontend or {}).get("dir") or "",
        "frontend_build": bool((frontend or {}).get("build_required")),
        "frontend_output": (frontend.get("static_output_dir") or ".") if committed_output else "",
        "frontend_api_env": list((frontend or {}).get("api_url_env") or []),
        "frontend_api_suffix": "" if (frontend or {}).get("appends_api_prefix") else "/api",
        "api_strip_prefix": bool(frontend) and not backend.get("uses_api_prefix", False),
        "price_class": settings.get("price_class", "PriceClass_100"),
        "database_engine": engine,
        "database_url_scheme": (db or {}).get("url_scheme") if engine else None,
        "db_instance_class": settings.get("db_instance_class", "db.t4g.micro"),
        "db_allocated_storage_gb": int(settings.get("db_allocated_storage_gb", 20)),
        "db_multi_az": bool(settings.get("db_multi_az", False)),
        "db_backup_retention_days": int(settings.get("db_backup_retention_days", 7)),
        "db_final_snapshot": bool(settings.get("db_final_snapshot", True)),
        # The template keeps CloudFront on regardless when there is a separate frontend.
        "cdn_enabled": bool(settings.get("cdn_enabled", True)),
        "run_migrations": bool(engine and layout.get("migration") and settings.get("run_migrations", True)),
        "secret_env_keys": secret_keys,
    }


def tfvars(target: str, deployment_id: str, region: str, environment: str,
           settings: Dict[str, Any], build: BuildResult, profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not _DEPLOYMENT_ID.match(deployment_id):
        raise ValueError("invalid deployment id")
    common = {"deployment_id": deployment_id, "region": region, "environment": environment}
    if target == "static_site":
        clean_sha = re.sub(r"[^a-zA-Z0-9_-]", "", build.package_sha256_b64 or "")[:12] or "v1"
        release_id = settings.get("release_id") or clean_sha
        return {
            **common,
            "release_id": release_id,
            "price_class": settings.get("price_class", "PriceClass_100"),
            "spa_mode": bool(settings.get("spa_mode", False)),
            "site_files": build.site_files,
        }
    if target == "lambda_http":
        return {
            **common,
            "runtime": build.runtime,
            "handler": build.handler,
            "memory_mb": int(settings.get("memory_mb", 256)),
            "timeout_s": int(settings.get("timeout_s", 30)),
            "public_url": bool(settings.get("public_url", True)),
            "package_file": build.package_file,
            "package_sha256_b64": build.package_sha256_b64,
        }
    if target == "ecs_service":
        return {
            **common,
            **_container_vars(settings, build),
            "certificate_arn": settings.get("certificate_arn") or None,
        }
    if target == "fullstack_app":
        return {
            **common,
            **_container_vars(settings, build),
            **_fullstack_vars(settings, profile or {}),
        }
    raise ValueError(f"Unknown deployment target '{target}'")


def render(target: str, deployment_id: str, region: str, environment: str,
           settings: Dict[str, Any], build: BuildResult, workdir: str,
           profile: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Write the project into `workdir` (next to the builder's site/ or
    artifacts/ output) and return its text files: the .tf files plus
    terraform.tfvars.json."""
    files = template_files(target)
    files[TFVARS_FILENAME] = json.dumps(
        tfvars(target, deployment_id, region, environment, settings, build, profile), indent=2, sort_keys=True
    ) + "\n"
    for name, content in files.items():
        with open(os.path.join(workdir, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    return files
