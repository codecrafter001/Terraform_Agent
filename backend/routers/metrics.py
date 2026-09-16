"""Prometheus metrics for TerraAgent: standard HTTP metrics via
prometheus-fastapi-instrumentator, plus custom counters/histograms for
pipeline-level outcomes that HTTP-layer instrumentation can't see (a scan
job runs across many Celery-worker minutes, well outside the request/response
cycle that generates the standard HTTP metrics)."""

import logging
from typing import Dict, Optional

from fastapi import FastAPI
from prometheus_client import Counter, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

logger = logging.getLogger(__name__)

jobs_total = Counter(
    "jobs_total",
    "Total scan jobs processed, by terminal status",
    ["status"]
)

agent_duration_seconds = Histogram(
    "agent_duration_seconds",
    "Per-agent wall-clock execution time within the LangGraph pipeline",
    ["agent"]
)

scan_errors_total = Counter(
    "scan_errors_total",
    "Total scan pipeline errors, by the agent active when the failure occurred",
    ["agent"]
)


def record_job_outcome(status: str, agent_timings: Optional[Dict[str, float]] = None) -> None:
    """Called once per job, at completion or failure, from the Celery task
    (and its inline-fallback counterpart) - the two places a job's terminal
    state is already known."""
    jobs_total.labels(status=status).inc()
    for agent, seconds in (agent_timings or {}).items():
        try:
            agent_duration_seconds.labels(agent=agent).observe(float(seconds))
        except (TypeError, ValueError):
            logger.warning(f"Skipping non-numeric agent timing for '{agent}': {seconds!r}")


def record_scan_error(agent: str) -> None:
    scan_errors_total.labels(agent=agent).inc()


def setup_metrics(app: FastAPI) -> None:
    """Wires standard HTTP request metrics (latency, in-progress, request
    count by handler/status) and exposes everything - HTTP + the custom
    counters/histograms above, since they share the same default registry -
    at GET /metrics."""
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
