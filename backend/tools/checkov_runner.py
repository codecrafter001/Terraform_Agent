"""Checkov runner for IaC static code analysis."""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List

from tools.sandbox_registry import create_sandbox, release_sandbox

logger = logging.getLogger("terraagent.checkov_runner")

CHECKOV_CONFIG_PATH = os.getenv("CHECKOV_CONFIG_PATH", "/app/security/checkov/.checkov.yaml")


class CheckovRunner:
    @staticmethod
    async def scan_hcl(hcl_files: Dict[str, str]) -> Dict[str, Any]:
        """Write HCL files to sandbox and run checkov with JSON output."""
        sandbox_dir = create_sandbox(prefix="terraagent_checkov_")
        findings: List[Dict[str, Any]] = []
        passed_count = 0
        skipped_count = 0
        tool_skipped = False

        try:
            for filename, content in hcl_files.items():
                file_path = os.path.join(sandbox_dir, filename)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

            cmd = ["checkov", "-d", sandbox_dir, "--framework", "terraform", "-o", "json", "--soft-fail"]
            if os.path.exists(CHECKOV_CONFIG_PATH):
                cmd.extend(["--config-file", CHECKOV_CONFIG_PATH])

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            output_str = stdout.decode("utf-8", errors="replace").strip()

            if output_str:
                try:
                    data = json.loads(output_str)
                    # Checkov may return a list or dict depending on framework scans
                    results_list = data if isinstance(data, list) else [data]
                    for report in results_list:
                        summary = report.get("summary", {}) or {}
                        passed_count += summary.get("passed", 0) or 0
                        skipped_count += summary.get("skipped", 0) or 0

                        failed_checks = report.get("results", {}).get("failed_checks", [])
                        for check in failed_checks:
                            findings.append({
                                "tool": "checkov",
                                "rule_id": check.get("check_id", "CKV_RULE"),
                                # Checkov's compliance-framework/CIS-benchmark mapping and real
                                # severity scoring require a paid Bridgecrew/Prisma Cloud API key -
                                # the open-source CLI always returns null for these, so this stays
                                # a sensible default rather than a real severity rating.
                                "severity": check.get("severity", "MEDIUM") or "MEDIUM",
                                "description": check.get("check_name", ""),
                                "resource": check.get("resource", ""),
                                "file": os.path.basename(check.get("file_path", "")),
                                "line": check.get("file_line_range", [0])[0] if check.get("file_line_range") else None,
                                "remediation": check.get("guideline", ""),
                                "bc_check_id": check.get("bc_check_id")
                            })
                except json.JSONDecodeError:
                    logger.warning("Could not parse checkov json output")

        except FileNotFoundError:
            logger.warning("checkov executable not found in PATH; skipping checkov scan.")
            tool_skipped = True
        except Exception as e:
            logger.error(f"checkov scan error: {e}")
        finally:
            release_sandbox(sandbox_dir)

        return {
            "tool": "checkov",
            "findings": findings,
            "total": len(findings),
            "passed": passed_count,
            "failed": len(findings),
            "skipped": skipped_count,
            "tool_skipped": tool_skipped
        }
