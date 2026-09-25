"""Conftest (OPA) runner for policy-as-code compliance scanning against Rego policies."""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from tools.sandbox_registry import create_sandbox, release_sandbox

# Fail closed: a scanner that hangs must count as a failed scan, never a clean one.
SCANNER_TIMEOUT_SECONDS = float(os.getenv("TERRAAGENT_SCANNER_TIMEOUT", "300"))

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
        tool_error: Optional[str] = None

        try:
            for filename, content in hcl_files.items():
                file_path = os.path.join(sandbox_dir, filename)
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
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
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=SCANNER_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise TimeoutError("conftest timed out after " + str(int(SCANNER_TIMEOUT_SECONDS)) + "s")
            output_str = stdout.decode("utf-8", errors="replace").strip()
            if not output_str and process.returncode:
                err_text = stderr.decode("utf-8", errors="replace").strip()[:200]
                tool_error = f"exited {process.returncode} with no output" + (f": {err_text}" if err_text else "")

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
                    tool_error = "unparseable JSON output"

        except FileNotFoundError:
            logger.warning("conftest executable not found in PATH; skipping OPA policy scan.")
            tool_skipped = True
        except Exception as e:
            logger.error(f"conftest scan error: {e}")
            tool_error = f"{type(e).__name__}: {e}"[:200]
        finally:
            release_sandbox(sandbox_dir)

        return {
            "tool": "conftest",
            "findings": findings,
            "total": len(findings),
            "passed": passed_count,
            "tool_skipped": tool_skipped,
            "tool_error": tool_error,
        }
