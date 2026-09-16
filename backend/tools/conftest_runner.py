"""Conftest (OPA) runner for policy-as-code compliance scanning against Rego policies."""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List

from tools.sandbox_registry import create_sandbox, release_sandbox

logger = logging.getLogger("terraagent.conftest_runner")

POLICY_DIR = os.getenv("CONFTEST_POLICY_DIR", "/app/security/policies")


class ConftestRunner:
    @staticmethod
    async def scan_hcl(hcl_files: Dict[str, str]) -> Dict[str, Any]:
        """Write HCL files to sandbox and run conftest against the OPA Rego policies."""
        sandbox_dir = create_sandbox(prefix="terraagent_conftest_")
        findings: List[Dict[str, Any]] = []
        passed_count = 0
        tool_skipped = False

        try:
            for filename, content in hcl_files.items():
                file_path = os.path.join(sandbox_dir, filename)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

            process = await asyncio.create_subprocess_exec(
                "conftest", "test", sandbox_dir,
                "--policy", POLICY_DIR,
                "--parser", "hcl2",
                "--output", "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            output_str = stdout.decode("utf-8", errors="replace").strip()

            if output_str:
                try:
                    data = json.loads(output_str)
                    for report in data:
                        source_file = os.path.basename(report.get("filename", ""))
                        passed_count += report.get("successes", 0) or 0
                        for failure in report.get("failures", []):
                            findings.append({
                                "tool": "conftest",
                                "rule_id": "OPA_POLICY",
                                "severity": "HIGH",
                                "description": failure.get("msg", str(failure)) if isinstance(failure, dict) else str(failure),
                                "resource": "",
                                "file": source_file,
                                "line": None,
                                "remediation": ""
                            })
                        for warning in report.get("warnings", []):
                            findings.append({
                                "tool": "conftest",
                                "rule_id": "OPA_POLICY_WARN",
                                "severity": "MEDIUM",
                                "description": warning.get("msg", str(warning)) if isinstance(warning, dict) else str(warning),
                                "resource": "",
                                "file": source_file,
                                "line": None,
                                "remediation": ""
                            })
                except json.JSONDecodeError:
                    logger.warning("Could not parse conftest json output")

        except FileNotFoundError:
            logger.warning("conftest executable not found in PATH; skipping OPA policy scan.")
            tool_skipped = True
        except Exception as e:
            logger.error(f"conftest scan error: {e}")
        finally:
            release_sandbox(sandbox_dir)

        return {
            "tool": "conftest",
            "findings": findings,
            "total": len(findings),
            "passed": passed_count,
            "tool_skipped": tool_skipped
        }
