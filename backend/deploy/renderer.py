"""Renders a deployment's Terraform project from TerraAgent's own templates
(design doc §6).

The template .tf files are copied verbatim; every user- or project-derived
value goes into terraform.tfvars.json - JSON, not HCL - so no string from an
upload can ever become Terraform syntax. No LLM is involved.
"""

import json
import os
import re
from typing import Any, Dict

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


def tfvars(target: str, deployment_id: str, region: str, environment: str,
           settings: Dict[str, Any], build: BuildResult) -> Dict[str, Any]:
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
            "container_port": int(settings.get("container_port") or build.container_port or 8080),
            "cpu": int(settings.get("cpu", 256)),
            "memory_mb": int(settings.get("memory_mb", 512)),
            "desired_count": int(settings.get("desired_count", 1)),
            "image_tag": str(settings.get("image_tag", "latest")),
            "certificate_arn": settings.get("certificate_arn"),
            "log_retention_days": int(settings.get("log_retention_days", 30)),
            "permissions_boundary_arn": settings.get("permissions_boundary_arn"),
        }
    raise ValueError(f"Unknown deployment target '{target}'")


def render(target: str, deployment_id: str, region: str, environment: str,
           settings: Dict[str, Any], build: BuildResult, workdir: str) -> Dict[str, str]:
    """Write the project into `workdir` (next to the builder's site/ or
    artifacts/ output) and return its text files: the .tf files plus
    terraform.tfvars.json."""
    files = template_files(target)
    files[TFVARS_FILENAME] = json.dumps(
        tfvars(target, deployment_id, region, environment, settings, build), indent=2, sort_keys=True
    ) + "\n"
    for name, content in files.items():
        with open(os.path.join(workdir, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    return files
