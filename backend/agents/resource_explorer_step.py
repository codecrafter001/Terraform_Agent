"""Discovery step: account-wide inventory via AWS Resource Explorer.

Runs before cloud_discovery. Resolves region="auto" to the region holding the
most supported resources, and warns when an explicitly chosen region has none
while other regions do (the usual cause of a "Complete, 0 resources" scan).
Best-effort: if Resource Explorer isn't available, discovery carries on with
the requested region exactly as before.
"""

import asyncio
import logging
import os
from typing import Any, Dict

from botocore.exceptions import ClientError

from services.redis_client import redis_service
from tools.resource_explorer import ResourceExplorerInventory, unavailable
from tools.sts_helper import assume_role

logger = logging.getLogger("terraagent.agents.resource_explorer")

AUTO_REGION = "auto"
FALLBACK_REGION = "us-east-1"


async def _log(job_id: str, message: str) -> None:
    await redis_service.publish_log(job_id, f"[AGENT:resource_explorer] {message}", agent_name="resource_explorer")


def _top(counts: Dict[str, int], n: int = 4) -> str:
    return ", ".join(f"{k} ({v})" for k, v in list(counts.items())[:n])


async def resource_explorer_node(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "")
    requested = (state.get("region") or FALLBACK_REGION).strip()
    is_auto = requested.lower() == AUTO_REGION
    query_region = FALLBACK_REGION if is_auto else requested
    creds = state.get("aws_credentials") or {}
    result: Dict[str, Any] = {"requested_region": requested}

    enabled = state.get("use_resource_explorer", True) is not False
    if not enabled or not (creds.get("access_key") and creds.get("secret_key")):
        inventory = unavailable("Skipped: turned off for this scan." if not enabled else "Skipped: no AWS credentials.")
    else:
        await _log(job_id, "Querying AWS Resource Explorer for an all-region inventory (read-only)...")
        access_key, secret_key, token = creds.get("access_key"), creds.get("secret_key"), creds.get("session_token")
        endpoint_url = state.get("aws_endpoint_url") or os.getenv("AWS_ENDPOINT_URL")
        role_arn = state.get("role_arn")
        inventory = None
        if role_arn:
            try:
                access_key, secret_key, token = assume_role(
                    role_arn, access_key, secret_key, token, query_region,
                    session_name=f"terraagent-{job_id}-rex", endpoint_url=endpoint_url,
                    external_id=state.get("external_id"),
                )
            except ClientError:
                # cloud_discovery reports the assume-role failure itself.
                inventory = unavailable("Skipped: could not assume the target role.")
        if inventory is None:
            explorer = ResourceExplorerInventory(access_key, secret_key, query_region, token, endpoint_url)
            inventory = await asyncio.to_thread(explorer.collect, state.get("resource_filters"))

    result["resource_inventory"] = inventory

    if inventory.get("available"):
        total = f"{inventory['total']}{'+' if inventory.get('truncated') else ''}"
        scope = "all regions" if inventory.get("aggregated") else f"{inventory.get('index_region')} only (no aggregator index)"
        await _log(
            job_id,
            f"Inventory: {total} resources across {scope}; {inventory['supported_total']} are types TerraAgent can "
            f"generate. By region: {_top(inventory['by_region'])}.",
        )
    else:
        await _log(job_id, f"Resource Explorer unavailable: {inventory.get('reason')}")

    suggested = inventory.get("suggested_region")
    supported_by_region = inventory.get("supported_by_region") or {}
    if is_auto:
        region = suggested or FALLBACK_REGION
        result["region"] = region
        if suggested:
            await _log(job_id, f"Auto region: scanning {region} ({supported_by_region.get(region, 0)} supported resources).")
        else:
            await _log(
                job_id,
                f"Auto region: no supported resources located, falling back to {FALLBACK_REGION}.",
            )
    elif suggested and supported_by_region.get(requested, 0) == 0:
        await _log(
            job_id,
            f"Warning: {requested} has no supported resources, but {_top(supported_by_region)} do. "
            f"Re-run the scan with one of those regions (or 'Auto').",
        )

    completed = list(state.get("completed_agents", []) or [])
    if "resource_explorer" not in completed:
        completed.append("resource_explorer")
    result.update({"completed_agents": completed, "current_agent": "cloud_discovery", "progress_percentage": 12})
    return result
