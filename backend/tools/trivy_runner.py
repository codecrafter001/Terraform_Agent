"""Trivy runner for IaC misconfiguration scanning."""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from tools.sandbox_registry import create_sandbox, release_sandbox

# Fail closed: a scanner that hangs must count as a failed scan, never a clean one.
SCANNER_TIMEOUT_SECONDS = float(os.getenv("TERRAAGENT_SCANNER_TIMEOUT", "300"))

logger = logging.getLogger("terraagent.trivy_runner")


class TrivyRunner:
    @staticmethod
    async def scan_hcl(hcl_files: Dict[str, str]) -> Dict[str, Any]:
        """Write HCL files to sandbox and run trivy config with JSON output."""
        sandbox_dir = create_sandbox(prefix="terraagent_trivy_")
        findings: List[Dict[str, Any]] = []
        tool_skipped = False
        tool_error: Optional[str] = None

        try:
            for filename, content in hcl_files.items():
                file_path = os.path.join(sandbox_dir, filename)
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
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
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=SCANNER_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise TimeoutError("trivy timed out after " + str(int(SCANNER_TIMEOUT_SECONDS)) + "s")
            output_str = stdout.decode("utf-8", errors="replace").strip()
            if not output_str and process.returncode:
                err_text = stderr.decode("utf-8", errors="replace").strip()[:200]
                tool_error = f"exited {process.returncode} with no output" + (f": {err_text}" if err_text else "")

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
                    tool_error = "unparseable JSON output"

        except FileNotFoundError:
            logger.warning("trivy executable not found in PATH; skipping trivy scan.")
            tool_skipped = True
        except Exception as e:
            logger.error(f"trivy scan error: {e}")
            tool_error = f"{type(e).__name__}: {e}"[:200]
        finally:
            release_sandbox(sandbox_dir)

        return {
            "tool": "trivy",
            "findings": findings,
            "total": len(findings),
            "tool_skipped": tool_skipped,
            "tool_error": tool_error,
        }
