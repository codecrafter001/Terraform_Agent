"""Infracost runner for estimating the monthly cost of generated Terraform/
OpenTofu HCL, before the final output is packaged. Same sandbox + tool_skipped
conventions as tfsec_runner.py/trivy_runner.py/checkov_runner.py/
conftest_runner.py - a missing binary or missing INFRACOST_API_KEY must never
silently read as "$0/month"; it's reported as unestimated, not as free.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List

from tools.sandbox_registry import create_sandbox, release_sandbox

logger = logging.getLogger("terraagent.infracost_runner")


def _to_float(value: Any) -> float:
    """Infracost's JSON reports costs as strings (e.g. "15.33"), not numbers -
    preserves full decimal precision across languages, per their own docs."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class InfracostRunner:
    @staticmethod
    async def estimate_cost(hcl_files: Dict[str, str]) -> Dict[str, Any]:
        """Write HCL files to sandbox and run `infracost breakdown` with JSON output."""
        sandbox_dir = create_sandbox(prefix="terraagent_infracost_")
        resources: List[Dict[str, Any]] = []
        total_monthly_cost = 0.0
        currency = "USD"
        unsupported_resource_count = 0
        tool_skipped = False

        try:
            for filename, content in hcl_files.items():
                file_path = os.path.join(sandbox_dir, filename)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

            if not os.getenv("INFRACOST_API_KEY"):
                logger.warning("INFRACOST_API_KEY not set; skipping cost estimation.")
                tool_skipped = True
            else:
                process = await asyncio.create_subprocess_exec(
                    "infracost", "breakdown",
                    "--path", sandbox_dir,
                    "--format", "json",
                    "--no-color",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                output_str = stdout.decode("utf-8", errors="replace").strip()

                if process.returncode != 0:
                    logger.warning(
                        f"infracost breakdown exited {process.returncode}; skipping cost "
                        f"estimation: {stderr.decode('utf-8', errors='replace').strip()}"
                    )
                    tool_skipped = True
                elif output_str:
                    try:
                        data = json.loads(output_str)
                        currency = data.get("currency", "USD")
                        total_monthly_cost = _to_float(data.get("totalMonthlyCost"))
                        for project in data.get("projects", []) or []:
                            for res in (project.get("breakdown", {}) or {}).get("resources", []) or []:
                                resources.append({
                                    "name": res.get("name", ""),
                                    "resource_type": res.get("resourceType", ""),
                                    "monthly_cost": _to_float(res.get("monthlyCost")),
                                })
                        summary = data.get("summary", {}) or {}
                        unsupported_resource_count = summary.get("totalUnsupportedResources", 0) or 0
                    except json.JSONDecodeError:
                        logger.warning("Could not parse infracost json output")
                        tool_skipped = True

        except FileNotFoundError:
            logger.warning("infracost executable not found in PATH; skipping cost estimation.")
            tool_skipped = True
        except Exception as e:
            logger.error(f"infracost cost estimation error: {e}")
            tool_skipped = True
        finally:
            release_sandbox(sandbox_dir)

        return {
            "tool": "infracost",
            "total_monthly_cost": total_monthly_cost,
            "currency": currency,
            "resources": sorted(resources, key=lambda r: r["monthly_cost"], reverse=True),
            "unsupported_resource_count": unsupported_resource_count,
            "tool_skipped": tool_skipped,
        }
