"""Deployment decision engine (design doc §5.2): ProjectProfile -> Decision.

An ordered, versioned rule table - no LLM. Every target is either eligible
(with the reasons it fits) or not (with the reason it doesn't), so the UI can
explain both. A user may override the recommendation only with another
eligible target.
"""

from typing import Dict, List, Literal, Optional, TypedDict

from deploy.analyzer import ProjectProfile
from deploy.config import MAX_LAMBDA_ZIP_BYTES

DECISION_RULES_VERSION = 2

TargetType = Literal["static_site", "lambda_http", "ecs_service"]
TARGETS: Dict[str, str] = {
    "static_site": "Static site: S3 + CloudFront",
    "lambda_http": "Function: AWS Lambda + HTTPS function URL",
    "ecs_service": "Container: ECS Fargate + load balancer",
}


class Reason(TypedDict):
    code: str
    target: Optional[str]
    message: str


class Decision(TypedDict):
    rules_version: int
    eligible: List[str]
    recommended: Optional[str]
    reasons: List[Reason]
    blocked: bool


def _reason(code: str, target: Optional[str], message: str) -> Reason:
    return {"code": code, "target": target, "message": message}


def _lambda_check(p: ProjectProfile) -> List[Reason]:
    """Reasons the project can't be a Lambda function; empty = eligible."""
    problems: List[Reason] = []
    if not p["lambda_handler"]:
        problems.append(_reason("lambda.no_handler", "lambda_http", "No Lambda handler found (handler(event, context) or exports.handler)."))
        return problems
    manifest = p["dependency_manifest"] or ""
    if manifest.startswith("unsupported:"):
        problems.append(_reason(
            "lambda.unsupported_manifest", "lambda_http",
            f"Dependencies are declared in {manifest.split(':', 1)[1]}; add a requirements.txt (or [project].dependencies).",
        ))
    if p["runtime"] == "node" and p["dependencies"] and not p["has_lockfile"]:
        problems.append(_reason("lambda.no_lockfile", "lambda_http", "Commit package-lock.json so dependencies install reproducibly."))
    if p["native_dependencies"]:
        problems.append(_reason(
            "lambda.native_dependencies", "lambda_http",
            f"These dependencies need a native build, which isn't supported yet: {', '.join(p['native_dependencies'])}.",
        ))
    if p["source_bytes"] > MAX_LAMBDA_ZIP_BYTES:
        problems.append(_reason("lambda.too_large", "lambda_http", "The source alone is larger than Lambda's 50 MB zip limit."))
    return problems


def blocked_by_secrets() -> Decision:
    return {
        "rules_version": DECISION_RULES_VERSION, "eligible": [], "recommended": None, "blocked": True,
        "reasons": [_reason("blocked.secrets", None, "Credentials were found in the source. Remove them and upload again.")],
    }


def decide(profile: ProjectProfile) -> Decision:
    reasons: List[Reason] = []
    eligible: List[str] = []

    lambda_problems = _lambda_check(profile)
    if not lambda_problems:
        eligible.append("lambda_http")
        reasons.append(_reason("lambda.handler_detected", "lambda_http", f"Lambda handler '{profile['lambda_handler']}' detected."))
    elif profile["lambda_handler"]:
        reasons.extend(lambda_problems)

    if profile["static_output_dir"] is not None and not profile["lambda_handler"] and not profile["server_entrypoint"]:
        eligible.append("static_site")
        where = profile["static_output_dir"] or "the project root"
        reasons.append(_reason("static.output_present", "static_site", f"Static site with index.html in {where}."))

    if profile["has_dockerfile"]:
        if "ecs_service" not in eligible:
            eligible.append("ecs_service")
        reasons.append(_reason("container.dockerfile_detected", "ecs_service", "A Dockerfile was found; eligible for ECS Fargate container deployment."))
    elif profile["server_entrypoint"] and not profile["lambda_handler"]:
        if "ecs_service" not in eligible:
            eligible.append("ecs_service")
        reasons.append(_reason("server.container_detected", "ecs_service", "A server entrypoint was found; eligible for ECS Fargate container deployment."))

    if profile["build_required"]:
        reasons.append(_reason(
            "build.unsafe_in_v1", "static_site",
            "The site needs a build step (npm run build), which TerraAgent doesn't run yet. Commit the build output "
            "(dist/, build/ or out/) and upload again.",
        ))
    if not eligible and not any(r["target"] for r in reasons):
        reasons.append(_reason("unknown.project_type", None, "Couldn't recognise a static site, Lambda function, or container in this project."))

    # Order of preference: a detected handler means Lambda, container server means ECS, otherwise static site.
    recommended = next((t for t in ("lambda_http", "ecs_service", "static_site") if t in eligible), None)
    return {
        "rules_version": DECISION_RULES_VERSION,
        "eligible": eligible,
        "recommended": recommended,
        "reasons": reasons,
        "blocked": False,
    }
