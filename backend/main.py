import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Load .env configuration
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(env_path)

from fastapi import FastAPI, Request, status

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from routers.download import router as download_router
from routers.jobs import router as jobs_router
from routers.metrics import setup_metrics
from routers.organizations import router as organizations_router
from routers.scan import router as scan_router
from services.auth import warn_if_unset as warn_if_api_key_unset
from services.database import init_db
from services.rate_limiter import limiter
from services.redis_client import redis_service
from tools.credential_scrubber import CredentialScrubber

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("terraagent.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing TerraAgent API Gateway...")
    # Initialize background connection pools
    try:
        client = await redis_service.get_client()
        if client:
            logger.info("Redis connection established.")
        else:
            logger.info("Redis unavailable, running with in-memory state & log queue.")
    except Exception as e:
        logger.info(f"Redis initialization info: {e}")

    init_db()
    logger.info("Database initialized.")

    warn_if_api_key_unset()


    yield

    logger.info("Shutting down TerraAgent API Gateway...")
    await redis_service.close()


app = FastAPI(
    title="TerraAgent API",
    description="Stateful Multi-Agent AWS Infrastructure to Terraform Synthesizer",
    version="1.0.0",
    lifespan=lifespan
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS Configuration
cors_origins_env = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost,http://127.0.0.1:3000")
origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler guaranteeing AWS credentials are never leaked in 500 errors.

    The scrubbed message is logged server-side only, never returned in the
    response body - even scrubbed, raw exception text (file paths, library
    internals, stack fragments) is an information-disclosure aid to a caller
    probing a production API and has no legitimate use on the client side.
    """
    scrubbed_msg = CredentialScrubber.scrub_text(str(exc))
    logger.error(f"Unhandled exception on {request.url.path}: {scrubbed_msg}")

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."}
    )


@app.get("/api/health", tags=["health"])
async def health_check():
    """Liveness check for Docker healthchecks."""
    return {
        "status": "healthy",
        "service": "terraagent-api",
        "version": "1.0.0"
    }


# Mount API routers under /api
app.include_router(scan_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")
app.include_router(download_router, prefix="/api")
app.include_router(organizations_router, prefix="/api")

# Prometheus metrics at GET /metrics (unprefixed - conventional scrape path)
setup_metrics(app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
