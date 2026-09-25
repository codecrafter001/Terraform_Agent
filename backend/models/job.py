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
    # Agent-level progress (infrastructure / iac_engineering / verification /
    # delivery) - see agents/graph.py.
    current_stage: Optional[str] = None
    completed_stages: List[str] = []
    stage_summaries: Dict[str, str] = {}
    verification_iterations: List[Dict[str, Any]] = []
    repair_attempts: int = 0
    max_repair_iterations: Optional[int] = None
    verification_verdict: Optional[str] = None
    repair_history: List[Dict[str, Any]] = []
    migration_confidence: Optional[Dict[str, Any]] = None
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
    resource_inventory: Dict[str, Any] = {}
    infra_model: Dict[str, Any] = {}
    requested_region: Optional[str] = None
    generation_manifest: Optional[Dict[str, Any]] = None
    validation_results: Dict[str, Any] = {}
    drift_results: Dict[str, Any] = {}
    plan_equivalence_results: Dict[str, Any] = {}
    security_results: Dict[str, Any] = {}
    migration_confidence: Optional[Dict[str, Any]] = None
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
