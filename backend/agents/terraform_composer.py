"""Terraform / OpenTofu Composer Agent: Generates clean, modular, deterministic IaC.

Consumes the P1 infrastructure intelligence (classification results + adoption plan +
dependency graph) and synthesizes verified HCL using the P2 HCLGenerator and IaCEngine.
"""

import logging
from typing import Any, Dict

from services.redis_client import redis_service
from tools.hcl_generator import HCLGenerator
from tools.iac_engine import get_iac_engine

logger = logging.getLogger(__name__)


async def terraform_composer_node(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "unknown")
    region = state.get("region", "us-east-1")
    resources = state.get("resources", [])
    classification_results = state.get("classification_results", {})
    adoption_plan = state.get("adoption_plan", {})
    dependency_graph = state.get("dependency_graph", {})
    binary_choice = state.get("terraform_binary", "terraform")

    engine = get_iac_engine(binary_choice)
    engine_version = await engine.get_version()

    await redis_service.publish_log(
        job_id,
        f"[AGENT:terraform_composer] Synthesizing modular {engine.name.upper()} HCL configuration...",
        agent_name="terraform_composer"
    )

    generator = HCLGenerator(
        job_id=job_id,
        region=region,
        engine_name=engine.name,
        engine_version=engine_version
    )

    terraform_files, manifest = generator.generate_project(
        resources=resources,
        classification_results=classification_results,
        adoption_plan=adoption_plan,
        dependency_graph=dependency_graph
    )

    # Reformat HCL canonically via the selected IaC engine
    terraform_files = await engine.format_hcl(terraform_files)

    await redis_service.publish_log(
        job_id,
        f"[AGENT:terraform_composer] {engine.name.upper()} synthesis complete: {len(terraform_files)} file(s) generated "
        f"({manifest.resources_generated} managed resources, {manifest.resources_data_source} data sources, "
        f"{manifest.resources_skipped} skipped, {manifest.resources_review_required} review required).",
        agent_name="terraform_composer"
    )

    completed_agents = list(state.get("completed_agents", []))
    if "terraform_composer" not in completed_agents:
        completed_agents.append("terraform_composer")

    return {
        "terraform_files": terraform_files,
        "generation_manifest": manifest.model_dump(),
        "completed_agents": completed_agents,
        "current_agent": "validation_agent",
        "progress_percentage": 60
    }
