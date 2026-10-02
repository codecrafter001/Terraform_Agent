"""Limits and locations for deployment mode, all environment-overridable."""

import os

_MB = 1024 * 1024


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# Source intake (design doc §9.1). Reads are streamed and capped; zip headers
# are never trusted for sizes.
MAX_UPLOAD_BYTES = _int_env("TERRAAGENT_DEPLOY_MAX_UPLOAD_MB", 25) * _MB
MAX_ARCHIVE_ENTRIES = 5000
MAX_FILE_BYTES = 10 * _MB
MAX_EXTRACTED_BYTES = 200 * _MB
MAX_PATH_LENGTH = 300
GITHUB_DOWNLOAD_TIMEOUT_SECONDS = 60.0

# Build (§9.2) and targets.
BUILD_TIMEOUT_SECONDS = float(_int_env("TERRAAGENT_BUILD_TIMEOUT", 300))
MAX_STATIC_FILES = 2000
MAX_LAMBDA_ZIP_BYTES = 50 * _MB
MAX_ANALYZED_SOURCE_FILES = 400  # handler/port detection reads at most this many source files

# Artifacts (§12). OUTPUT_DIR is the volume shared by the API and the workers.
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/tmp/terraagent")
ARTIFACT_ROOT = os.getenv("TERRAAGENT_ARTIFACT_ROOT", os.path.join(OUTPUT_DIR, "artifacts"))
SOURCE_RETENTION_DAYS = _int_env("TERRAAGENT_DEPLOY_SOURCE_RETENTION_DAYS", 7)
BUILD_RETENTION_DAYS = _int_env("TERRAAGENT_DEPLOY_BUILD_RETENTION_DAYS", 30)

# A deployment stuck before apply (worker lost) is failed after this long -
# safe, because nothing before apply changes AWS.
MAX_STAGE_RUNTIME_SECONDS = _int_env("MAX_JOB_RUNTIME_SECONDS", 1800)
