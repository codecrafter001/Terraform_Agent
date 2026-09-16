"""Pydantic model for generation_manifest.json produced by the Terraform/OpenTofu engine."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class UnresolvedAttribute(BaseModel):
    resource_id: str
    resource_type: str
    attribute_name: str
    reason: str


class GenerationManifest(BaseModel):
    job_id: str
    engine: str  # "terraform" | "opentofu"
    engine_version: Optional[str] = None
    generator_version: str = "2.0.0"
    generated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    region: str = "us-east-1"
    resources_discovered: int = 0
    resources_generated: int = 0
    resources_skipped: int = 0
    resources_data_source: int = 0
    resources_review_required: int = 0
    resources_unsupported: int = 0
    resources_failed: int = 0
    adoption_outcomes: Dict[str, str] = Field(default_factory=dict)
    unresolved_attributes: List[UnresolvedAttribute] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    validation_status: Literal["PASS", "FAIL", "PARTIAL", "PENDING"] = "PENDING"
    generated_files: List[str] = Field(default_factory=list)
