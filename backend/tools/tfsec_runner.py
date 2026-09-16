"""Tfsec runner for scanning Terraform HCL security policies."""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List

from tools.sandbox_registry import create_sandbox, release_sandbox

logger = logging.getLogger("terraagent.tfsec_runner")

TFSEC_CONFIG_PATH = os.getenv("TFSEC_CONFIG_PATH", "/app/security/.tfsec/config.yml")


class TfsecRunner:
    @staticmethod
    async def scan_hcl(hcl_files: Dict[str, str]) -> Dict[str, Any]:
        """Write HCL files to sandbox and run tfsec with JSON output."""
        sandbox_dir = create_sandbox(prefix="terraagent_tfsec_")
        findings: List[Dict[str, Any]] = []
        tool_skipped = False

        try:
            for filename, content in hcl_files.items():
                file_path = os.path.join(sandbox_dir, filename)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

            cmd = ["tfsec", sandbox_dir, "--format", "json", "--soft-fail"]
            if os.path.exists(TFSEC_CONFIG_PATH):
                cmd.extend(["--config-file", TFSEC_CONFIG_PATH])

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
                    raw_results = data.get("results") or []
                    for item in raw_results:
                        findings.append({
                            "tool": "tfsec",
                            "rule_id": item.get("rule_id", "TFSEC_RULE"),
                            "severity": item.get("severity", "MEDIUM").upper(),
                            "description": item.get("description", ""),
                            "resource": item.get("resource", ""),
                            "file": os.path.basename(item.get("location", {}).get("filename", "")),
                            "line": item.get("location", {}).get("start_line"),
                            "remediation": item.get("resolution", "")
                        })
                except json.JSONDecodeError:
                    logger.warning(f"Could not parse tfsec json output: {output_str}")

        except FileNotFoundError:
            logger.warning("tfsec executable not found in PATH; skipping tfsec scan.")
            tool_skipped = True
        except Exception as e:
            logger.error(f"tfsec scan error: {e}")
        finally:
            release_sandbox(sandbox_dir)

        return {
            "tool": "tfsec",
            "findings": findings,
            "total": len(findings),
            "tool_skipped": tool_skipped
        }
