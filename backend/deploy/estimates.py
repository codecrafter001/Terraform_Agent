"""Presets and a quick cost/time estimate for the full-stack target
(docs/design/fast-deploy-and-service-selection.md, phase 3).

The estimate is for choosing settings, before anything is built: approximate
us-east-1 on-demand list prices and observed AWS creation times. After Build,
Infracost prices the actual Terraform; that number is the one to trust. No AWS
call and no LLM is involved.
"""

from typing import Any, Dict, List, Optional, Tuple, TypedDict

HOURS_PER_MONTH = 730

# Approximate us-east-1 on-demand list prices (USD). Other regions are typically 0-30% higher.
FARGATE_VCPU_HOUR = 0.04048
FARGATE_GB_HOUR = 0.004445
ALB_MONTH = 0.0225 * HOURS_PER_MONTH + 3.0  # hourly charge + light LCU usage
PUBLIC_IPV4_MONTH = 0.005 * HOURS_PER_MONTH
RDS_HOURLY = {  # single-AZ; PostgreSQL and MySQL are priced alike at these sizes
    "db.t4g.micro": 0.016,
    "db.t4g.small": 0.032,
    "db.t4g.medium": 0.065,
    "db.t4g.large": 0.129,
    "db.m7g.large": 0.168,
}
GP3_GB_MONTH = 0.115
AURORA_ACU_HOUR = 0.12
AURORA_GB_MONTH = 0.10
VALKEY_SERVERLESS_MIN_MONTH = 0.084 * 0.1 * HOURS_PER_MONTH  # $0.084/GB-hour, 100 MB billed minimum
S3_UPLOADS_MONTH = 1.0  # a few GB of objects and requests
SECRET_MONTH = 0.40
CLOUDFRONT_MONTH = 1.0  # small sites stay within the always-free 1 TB / 10M requests
PIPELINE_LOGS_ECR_MONTH = 2.5  # CodeBuild minutes, CodePipeline, CloudWatch Logs, ECR storage

# Minutes (low, high) for the slow parts of a first deploy.
BASE_MINUTES = (3, 4)  # plan/apply overhead, VPC, IAM, S3, ECR, load balancer
DB_MINUTES = (8, 12)
DB_MULTI_AZ_MINUTES = (12, 18)
AURORA_MINUTES = (12, 18)  # cluster, then its serverless instance
CACHE_MINUTES = (5, 8)
CDN_MINUTES = (4, 8)
IMAGE_BUILD_MINUTES = (4, 7)
FRONTEND_BUILD_MINUTES = (1, 3)
SERVICE_HEALTHY_MINUTES = (1, 2)

PRESETS: Dict[str, Dict[str, Any]] = {
    "dev": {
        "cdn_enabled": False, "cpu": 256, "memory_mb": 512, "desired_count": 1,
        "db_instance_class": "db.t4g.micro", "db_allocated_storage_gb": 20, "db_multi_az": False,
        "db_backup_retention_days": 1, "db_final_snapshot": False, "autoscaling_max_count": None,
    },
    "staging": {
        "cdn_enabled": True, "cpu": 256, "memory_mb": 512, "desired_count": 1,
        "db_instance_class": "db.t4g.micro", "db_allocated_storage_gb": 20, "db_multi_az": False,
        "db_backup_retention_days": 3, "db_final_snapshot": False, "autoscaling_max_count": None,
    },
    "production": {
        "cdn_enabled": True, "cpu": 512, "memory_mb": 1024, "desired_count": 2, "autoscaling_max_count": 4,
        "db_instance_class": "db.t4g.small", "db_allocated_storage_gb": 20, "db_multi_az": False,
        "db_backup_retention_days": 7, "db_final_snapshot": True,
    },
}
PRESET_DESCRIPTIONS = {
    "dev": "Fastest and cheapest: no CloudFront (HTTP load-balancer URL), smallest sizes, 1-day backups.",
    "staging": "Like production at the smallest sizes: CloudFront HTTPS URL, 3-day backups.",
    "production": "Two app tasks scaling to four, a larger database, 7-day backups and a final snapshot on teardown.",
}


class CostLine(TypedDict):
    item: str
    monthly_usd: float


class Estimate(TypedDict):
    monthly_usd: float
    lines: List[CostLine]
    minutes_low: int
    minutes_high: int
    cdn: bool
    notes: List[str]


def preset_for_environment(environment: Optional[str]) -> str:
    env = (environment or "").lower()
    if env in ("production", "prod"):
        return "production"
    if env in ("staging", "stage", "preprod"):
        return "staging"
    return "dev"


def _add(a: Tuple[int, int], b: Tuple[int, int]) -> Tuple[int, int]:
    return a[0] + b[0], a[1] + b[1]


def estimate_fullstack(settings: Dict[str, Any], layout: Optional[Dict[str, Any]]) -> Estimate:
    """Monthly cost and first-deploy time for FullstackSettings-shaped `settings`."""
    layout = layout or {}
    frontend = layout.get("frontend")
    db = layout.get("database")
    cdn = bool(settings.get("cdn_enabled", True) or frontend)
    mode = settings.get("database", "rds")
    rds = mode == "rds" and bool(db and db.get("rds_supported"))
    aurora = mode == "aurora" and bool(db and db.get("rds_supported"))
    def addon(value, detected):
        return bool(detected) if value is None else bool(value)
    cache = addon(None if settings.get("cache") is None else settings.get("cache") == "valkey", layout.get("cache"))
    uploads = addon(settings.get("uploads_bucket"), layout.get("object_storage"))
    worker = bool(layout.get("worker")) and addon(settings.get("worker_enabled"), layout.get("worker"))
    max_tasks = max(int(settings.get("desired_count", 1)), int(settings.get("autoscaling_max_count") or 0))
    tasks = int(settings.get("desired_count", 1))
    vcpu = int(settings.get("cpu", 256)) / 1024
    gb = int(settings.get("memory_mb", 512)) / 1024
    multi_az = bool(settings.get("db_multi_az")) and rds
    keys = settings.get("secret_env_keys")
    secrets = len(keys if keys is not None else (layout.get("env_keys") or []))
    if settings.get("database") == "external" and not (keys and "DATABASE_URL" in keys):
        secrets += 1

    lines: List[CostLine] = [
        {"item": f"App containers ({tasks} x {vcpu:g} vCPU / {gb:g} GB, Fargate)",
         "monthly_usd": tasks * (vcpu * FARGATE_VCPU_HOUR + gb * FARGATE_GB_HOUR) * HOURS_PER_MONTH},
        {"item": "Load balancer", "monthly_usd": ALB_MONTH},
        {"item": f"Public IPv4 addresses ({2 + tasks})", "monthly_usd": (2 + tasks) * PUBLIC_IPV4_MONTH},
    ]
    if rds:
        db_class = settings.get("db_instance_class", "db.t4g.micro")
        storage = int(settings.get("db_allocated_storage_gb", 20))
        factor = 2 if multi_az else 1
        engine = "MySQL" if db and db.get("engine") == "mysql" else "PostgreSQL"
        lines.append({"item": f"{engine} {db_class}{' Multi-AZ' if multi_az else ''}, {storage} GB",
                      "monthly_usd": factor * (RDS_HOURLY.get(db_class, RDS_HOURLY["db.t4g.micro"]) * HOURS_PER_MONTH
                                               + storage * GP3_GB_MONTH)})
        secrets += 1  # the RDS-managed password secret
    if aurora:
        min_acu = float(settings.get("aurora_min_acu", 0.5))
        engine = "MySQL" if db and db.get("engine") == "mysql" else "PostgreSQL"
        lines.append({"item": f"Aurora Serverless v2 {engine} ({min_acu:g}-{float(settings.get('aurora_max_acu', 4)):g} ACU, at minimum)",
                      "monthly_usd": min_acu * AURORA_ACU_HOUR * HOURS_PER_MONTH + 20 * AURORA_GB_MONTH})
        secrets += 1
    if cache:
        lines.append({"item": "Valkey cache (ElastiCache Serverless, minimum)", "monthly_usd": VALKEY_SERVERLESS_MIN_MONTH})
    if uploads:
        lines.append({"item": "S3 uploads bucket (light use)", "monthly_usd": S3_UPLOADS_MONTH})
    if worker:
        lines.append({"item": f"Background worker (1 x {vcpu:g} vCPU / {gb:g} GB) + public IPv4",
                      "monthly_usd": (vcpu * FARGATE_VCPU_HOUR + gb * FARGATE_GB_HOUR) * HOURS_PER_MONTH + PUBLIC_IPV4_MONTH})
    if cdn:
        lines.append({"item": "CloudFront (light traffic)", "monthly_usd": CLOUDFRONT_MONTH})
    if secrets:
        lines.append({"item": f"Secrets Manager ({secrets} secrets)", "monthly_usd": secrets * SECRET_MONTH})
    lines.append({"item": "Build pipeline, logs, image storage", "monthly_usd": PIPELINE_LOGS_ECR_MONTH})
    for line in lines:
        line["monthly_usd"] = round(line["monthly_usd"], 2)

    # Critical path of a first deploy: the image builds in parallel with the database and
    # CloudFront; the service starts once the database (if any) and the image exist; the
    # frontend is published once its distribution exists.
    image_ready = _add((1, 1), IMAGE_BUILD_MINUTES)
    db_ready = (DB_MULTI_AZ_MINUTES if multi_az else DB_MINUTES) if rds else AURORA_MINUTES if aurora else (0, 0)
    if cache:  # the containers get REDIS_URL, so they wait for the cache too
        db_ready = (max(db_ready[0], CACHE_MINUTES[0]), max(db_ready[1], CACHE_MINUTES[1]))
    service_path = _add((max(BASE_MINUTES[0], db_ready[0], image_ready[0]), max(BASE_MINUTES[1], db_ready[1], image_ready[1])),
                        SERVICE_HEALTHY_MINUTES)
    total = service_path
    if frontend:
        fe_build = FRONTEND_BUILD_MINUTES if frontend.get("build_required") else (0, 1)
        fe_path = _add((max(CDN_MINUTES[0], image_ready[0]), max(CDN_MINUTES[1], image_ready[1])), fe_build)
        total = (max(total[0], fe_path[0]), max(total[1], fe_path[1]))
    elif cdn:
        total = (max(total[0], CDN_MINUTES[0] + 1), max(total[1], CDN_MINUTES[1] + 1))

    notes = ["Approximate us-east-1 list prices; Infracost prices the generated Terraform after Build."]
    if not cdn:
        notes.append("Without CloudFront the app is served over plain HTTP from the load balancer URL.")
    if settings.get("cdn_enabled") is False and frontend:
        notes.append("CloudFront stays on: the separate frontend is served from a private S3 bucket through it.")
    if secrets:
        notes.append("The app starts once every secret has a value.")
    if max_tasks > tasks:
        notes.append(f"Autoscaling can add up to {max_tasks - tasks} more app task(s) under load; each costs about "
                     f"${(vcpu * FARGATE_VCPU_HOUR + gb * FARGATE_GB_HOUR) * HOURS_PER_MONTH + PUBLIC_IPV4_MONTH:.0f}/month while running.")
    if aurora and float(settings.get("aurora_min_acu", 0.5)) == 0:
        notes.append("Aurora pauses after 5 idle minutes (no compute charge while paused); the first request after a pause waits ~15 s.")
    return {
        "monthly_usd": round(sum(line["monthly_usd"] for line in lines), 2),
        "lines": lines,
        "minutes_low": total[0],
        "minutes_high": total[1],
        "cdn": cdn,
        "notes": notes,
    }
