"""Workspace settings: the effective, read-only configuration of this
deployment, plus a few editable non-secret defaults.

The configuration half is reported from the same module constants the
pipeline uses, so it can't drift from what actually runs. Secrets are only
ever reported as set / not set - never their values. The editable defaults
(region, resource filters, GitHub repo, base branch, engine) pre-fill the
scan and PR forms; they are validated against fixed patterns and never hold
credentials or tokens.
"""

import os
import re
import shutil
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from services.auth import require_api_key
from services.database import get_app_settings, set_app_settings
from tools.github_repo import normalize_repo

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_api_key)])

# The resource_filters categories AWSScanner and the forms know about.
KNOWN_FILTERS = ("EC2", "ECS", "VPC", "SG", "S3", "RDS", "IAM", "ELB", "DYNAMODB", "KMS", "SQS", "SNS")
_REGION = re.compile(r"^(auto|[a-z]{2}(-gov)?-[a-z]+-\d)$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
_TOOLS = ("terraform", "tofu", "checkov", "trivy", "conftest", "infracost")


class WorkspaceDefaults(BaseModel):
    region: str = "auto"
    resource_filters: List[str] = Field(default_factory=lambda: [f for f in KNOWN_FILTERS if f != "ECS"])
    terraform_binary: Literal["terraform", "tofu"] = "terraform"
    github_repo: Optional[str] = None
    base_branch: str = "main"

    @field_validator("region")
    @classmethod
    def _region(cls, v: str) -> str:
        if not _REGION.match(v):
            raise ValueError("region must be 'auto' or an AWS region like us-east-1")
        return v

    @field_validator("resource_filters")
    @classmethod
    def _filters(cls, v: List[str]) -> List[str]:
        unknown = sorted(set(v) - set(KNOWN_FILTERS))
        if unknown:
            raise ValueError(f"unknown resource filters: {', '.join(unknown)}")
        return [f for f in KNOWN_FILTERS if f in v]

    @field_validator("github_repo")
    @classmethod
    def _repo(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip() or None
        return normalize_repo(v) if v else None

    @field_validator("base_branch")
    @classmethod
    def _branch(cls, v: str) -> str:
        if not _BRANCH.match(v) or ".." in v or v.startswith("/") or v.endswith("/"):
            raise ValueError("base_branch is not a valid branch name")
        return v


def _defaults() -> WorkspaceDefaults:
    stored = get_app_settings().get("defaults") or {}
    try:
        return WorkspaceDefaults(**stored)
    except ValueError:
        # A stored value that no longer validates (e.g. a filter was removed) -
        # fall back to the defaults rather than break the settings page.
        return WorkspaceDefaults()


def _configuration() -> Dict[str, Any]:
    from agents.graph import HEARTBEAT_INTERVAL_SECONDS, MAX_REPAIR_ITERATIONS
    from services.auth import _API_KEY
    from services.celery_app import MAX_JOB_RUNTIME_SECONDS, ZIP_EXPIRY_SECONDS
    from tools.adoption_planner import DEPENDENCY_CONFIDENCE_THRESHOLD, MAX_ADOPTION_WAVE_SIZE
    from tools.checkov_runner import SCANNER_TIMEOUT_SECONDS
    from tools.resource_classifier import MANAGE_IAM
    from tools.terraform_runner import ALLOWED_SUBCOMMANDS, TERRAFORM_COMMAND_TIMEOUT_SECONDS

    return {
        "safety": {
            "allowed_terraform_subcommands": sorted(ALLOWED_SUBCOMMANDS),
            "blocked_terraform_commands": ["apply", "destroy", "import"],
            "aws_api_access": "Describe*, Get*, List* only (plus sts:AssumeRole)",
            "api_key_required": bool(_API_KEY),
        },
        "pipeline": {
            "max_repair_iterations": MAX_REPAIR_ITERATIONS,
            "manage_iam_roles": MANAGE_IAM,
            "terraform_command_timeout_seconds": TERRAFORM_COMMAND_TIMEOUT_SECONDS,
            "scanner_timeout_seconds": SCANNER_TIMEOUT_SECONDS,
            "heartbeat_seconds": HEARTBEAT_INTERVAL_SECONDS,
            "max_job_runtime_seconds": MAX_JOB_RUNTIME_SECONDS,
            "max_adoption_wave_size": MAX_ADOPTION_WAVE_SIZE,
            "dependency_confidence_threshold": DEPENDENCY_CONFIDENCE_THRESHOLD,
            "bundle_expiry_hours": ZIP_EXPIRY_SECONDS // 3600,
        },
        "llm": {"provider": "ollama", "model": os.getenv("OLLAMA_MODEL", "codellama")},
        "integrations": {
            # Whether a key is configured - never its value.
            "infracost_api_key_set": bool(os.getenv("INFRACOST_API_KEY")),
        },
        # Found on PATH in the API container (the worker is built from the same image).
        "tools": {tool: shutil.which(tool) is not None for tool in _TOOLS},
    }


@router.get("")
async def get_settings() -> Dict[str, Any]:
    return {"defaults": _defaults().model_dump(), "configuration": _configuration()}


@router.put("/defaults")
async def update_defaults(defaults: WorkspaceDefaults) -> Dict[str, Any]:
    set_app_settings({"defaults": defaults.model_dump()})
    return {"defaults": defaults.model_dump()}
