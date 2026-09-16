"""Models for Job progress, results and status details."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from .scan import JobStatus, OperationType


class JobProgress(BaseModel):
    job_id: str
    status: JobStatus
    progress_percentage: int = 0
    current_agent: Optional[str] = None
    completed_agents: List[str] = []
    created_at: str
    updated_at: Optional[str] = None
    error: Optional[str] = None


class JobResults(BaseModel):
    job_id: str
    status: JobStatus
    operation: OperationType
    region: str
    resources_count: int = 0
    resources: List[Dict[str, Any]] = []
    classification_results: Dict[str, Any] = {}
    dependency_graph: Dict[str, Any] = {}
    adoption_plan: Dict[str, Any] = {}
    generation_manifest: Optional[Dict[str, Any]] = None
    validation_results: Dict[str, Any] = {}
    drift_results: Dict[str, Any] = {}
    plan_equivalence_results: Dict[str, Any] = {}
    security_results: Dict[str, Any] = {}
    pending_approval: Optional[Dict[str, Any]] = None
    approval_decision: Optional[Dict[str, Any]] = None
    github_pr: Optional[Dict[str, Any]] = None
    github_wave_prs: Dict[str, Any] = {}
    zip_available: bool = False
    download_url: Optional[str] = None
    zip_sha256: Optional[str] = None
    zip_manifest: List[Dict[str, Any]] = []


class JobDecisionResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: str


class PullRequestResponse(BaseModel):
    job_id: str
    pr_url: str
    pr_number: int
    branch: str
    wave: Optional[int] = None
