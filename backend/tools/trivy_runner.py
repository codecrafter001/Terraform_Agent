"""Trivy runner for IaC misconfiguration scanning."""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List

from tools.sandbox_registry import create_sandbox, release_sandbox

logger = logging.getLogger("terraagent.trivy_runner")


class TrivyRunner:
    @staticmethod
    async def scan_hcl(hcl_files: Dict[str, str]) -> Dict[str, Any]:
        """Write HCL files to sandbox and run trivy config with JSON output."""
        sandbox_dir = create_sandbox(prefix="terraagent_trivy_")
        findings: List[Dict[str, Any]] = []
        tool_skipped = False

        try:
            for filename, content in hcl_files.items():
                file_path = os.path.join(sandbox_dir, filename)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

            process = await asyncio.create_subprocess_exec(
                "trivy",
                "config",
                sandbox_dir,
                "--format", "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            output_str = stdout.decode("utf-8", errors="replace").strip()

            if output_str:
                try:
                    data = json.loads(output_str)
                    results = data.get("Results", [])
                    for res in results:
                        for misconf in res.get("Misconfigurations", []):
                            findings.append({
                                "tool": "trivy",
                                "rule_id": misconf.get("ID", "TRIVY_RULE"),
                                "severity": misconf.get("Severity", "MEDIUM").upper(),
                                "description": misconf.get("Title", misconf.get("Message", "")),
                                "resource": misconf.get("Query", ""),
                                "file": os.path.basename(res.get("Target", "")),
                                "line": misconf.get("CauseMetadata", {}).get("StartLine"),
                                "remediation": misconf.get("Resolution", "")
                            })
                except json.JSONDecodeError:
                    logger.warning("Could not parse trivy json output")

        except FileNotFoundError:
            logger.warning("trivy executable not found in PATH; skipping trivy scan.")
            tool_skipped = True
        except Exception as e:
            logger.error(f"trivy scan error: {e}")
        finally:
            release_sandbox(sandbox_dir)

        return {
            "tool": "trivy",
            "findings": findings,
            "total": len(findings),
            "tool_skipped": tool_skipped
        }
