"""Router for streaming generated ZIP packages."""

import os
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from services.auth import require_api_key
from services.redis_client import redis_service

router = APIRouter(prefix="/download", tags=["download"], dependencies=[Depends(require_api_key)])

# Must match routers/scan.py's job_id generation (f"job-{uuid.uuid4().hex[:12]}")
# exactly. job_id reaches os.path.join() below unescaped - without this check,
# a value like ".." resolves one directory above OUTPUT_DIR (verified:
# os.path.join("/tmp/terraagent", "..", "output.zip") normalizes to
# "/tmp/output.zip"). Reject anything that isn't a real job id before it
# ever touches the filesystem, rather than trying to sanitize it.
_JOB_ID_PATTERN = re.compile(r"^job-[0-9a-f]{12}$")


@router.get("/{job_id}")
async def download_zip(job_id: str):
    """Streams the generated Terraform ZIP package."""
    if not _JOB_ID_PATTERN.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="ZIP package not found for this job ID")

    state = await redis_service.get_job_state(job_id)
    zip_path = state.get("zip_path") if state else None

    # Fallback check on standard directory
    if not zip_path or not os.path.exists(zip_path):
        target_dir = os.getenv("OUTPUT_DIR", "/tmp/terraagent")
        candidate1 = os.path.join(target_dir, f"{job_id}.zip")
        candidate2 = os.path.join(target_dir, job_id, "output.zip")
        if os.path.exists(candidate1):
            zip_path = candidate1
        elif os.path.exists(candidate2):
            zip_path = candidate2


    if not zip_path or not os.path.exists(zip_path):
        raise HTTPException(status_code=404, detail="ZIP package not found for this job ID")

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"terraagent_{job_id}.zip"
    )
