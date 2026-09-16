"""Pydantic models for resource classification, adoption planning, and
plan-equivalence results.
"""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ResourceClassification(BaseModel):
    resource_id: str
    resource_type: str
    category: Literal["managed", "unmanaged", "drifted", "orphaned", "shared", "unsupported"]
    reason: List[str]
    recommended_action: Literal["import", "data_source", "skip", "manual_review"]


class ClassificationReport(BaseModel):
    classifications: List[ResourceClassification]
    summary: Dict[str, int]  # category -> count


class AdoptionCategoryPlan(BaseModel):
    category: Literal["safe_to_import", "review_required", "do_not_manage", "use_data_source", "unsupported"]
    resource_ids: List[str]
    resource_count: int


class AdoptionWave(BaseModel):
    """One sequential migration batch within the safe_to_import category,
    ordered by dependency depth on trusted edges."""
    wave: int  # 1-indexed
    category_name: str = "Application"  # e.g., Foundation, Security, Compute, Data, Application
    resource_ids: List[str]
    risk_level: Literal["low", "medium", "high"]
    risk_signals: List[str] = Field(default_factory=list)  # deterministic, human-readable reasons for risk_level


class AdoptionPlan(BaseModel):
    categories: List[AdoptionCategoryPlan]
    total_resource_count: int
    managed_count: int = 0  # safe_to_import count
    review_count: int = 0  # review_required count
    data_source_count: int = 0  # use_data_source count
    unsupported_count: int = 0  # unsupported count
    do_not_manage_count: int = 0  # do_not_manage count
    total_dependencies: int = 0  # total edge count in graph
    high_confidence_dependencies: int = 0  # trusted edge count (confidence >= threshold)
    wave_count: int = 0  # number of migration waves
    risk_score: int  # 0-100: percentage of resources in review_required + unsupported
    import_order: List[str]  # resource ids, topological order of safe_to_import
    waves: List[AdoptionWave] = Field(default_factory=list)  # safe_to_import resources grouped into migration stages
    cycles_detected: List[List[str]] = Field(default_factory=list)  # Any cyclic dependencies safely handled
    summary: Optional[str] = None  # LLM narrative, falls back to a deterministic template on failure


class PlanEquivalenceAction(BaseModel):
    address: str
    action: Literal["no-op", "create", "update", "replace", "destroy"]
    resource_type: str


class PlanEquivalenceResult(BaseModel):
    passed: bool
    confidence_score: int  # 0-100
    actions: List[PlanEquivalenceAction]
    blocking_actions: List[PlanEquivalenceAction]  # the replace/destroy subset
    raw_plan_summary: Dict[str, int]  # {"create": n, "update": n, "replace": n, "destroy": n, "no-op": n}

