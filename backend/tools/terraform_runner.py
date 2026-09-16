"""Terraform CLI runner for validating HCL code in sandboxed directories.
Enforces safety: only fmt, init (no backend), validate, plan and show are
permitted - apply/destroy/import are hard-blocked in run_command below.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from tools.credential_scrubber import CredentialScrubber
from tools.sandbox_registry import create_sandbox, release_sandbox

logger = logging.getLogger("terraagent.terraform_runner")


class TerraformRunner:
    @staticmethod
    async def run_command(
        cmd: List[str], cwd: str, env: Optional[Dict[str, str]] = None
    ) -> Tuple[int, str, str]:
        """Safely execute a CLI command in the specified working directory.

        env=None (the default, used by every caller except plan_json) means
        "inherit the parent process's environment unchanged" - the same
        behavior this always had. plan_json is the one caller that passes an
        explicit, minimal env dict scoped to a single subprocess call, since
        that's the only command in this file that ever needs real AWS
        credentials."""
        # Hard safety validation
        disallowed = ["apply", "destroy", "import"]
        for arg in cmd:
            if arg.lower() in disallowed:
                raise ValueError(f"Safety Violation: '{arg}' is strictly prohibited.")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        stdout, stderr = await process.communicate()
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace")
        )

    @classmethod
    async def format_hcl(cls, hcl_files: Dict[str, str], binary: str = "terraform") -> Dict[str, str]:
        """Canonically reformat HCL content via `<binary> fmt` (pure reformatting -
        never changes semantics, so it cannot introduce new bugs). Falls back to the
        original content for any file where fmt fails to run cleanly.

        binary is "terraform" or "tofu" (models/scan.py::ScanRequest.terraform_binary,
        threaded through TerraAgentState) - OpenTofu is a drop-in-compatible CLI fork
        of Terraform, same subcommands/flags/output shapes, so nothing else here
        needs to branch on which one is running."""
        sandbox_dir = create_sandbox(prefix="terraagent_fmt_")
        try:
            for filename, content in hcl_files.items():
                with open(os.path.join(sandbox_dir, filename), "w", encoding="utf-8") as f:
                    f.write(content)

            code, _, err = await cls.run_command([binary, "fmt"], cwd=sandbox_dir)
            if code != 0:
                logger.warning(f"{binary} fmt failed, keeping unformatted content: {err}")
                return hcl_files

            formatted = {}
            for filename in hcl_files:
                with open(os.path.join(sandbox_dir, filename), "r", encoding="utf-8") as f:
                    formatted[filename] = f.read()
            return formatted
        except Exception as e:
            logger.warning(f"{binary} fmt error, keeping unformatted content: {e}")
            return hcl_files
        finally:
            release_sandbox(sandbox_dir)

    @classmethod
    async def validate_hcl(cls, hcl_files: Dict[str, str], binary: str = "terraform") -> Dict[str, Any]:
        """Write HCL files to sandbox, run init and validate. See format_hcl's
        docstring for what `binary` means."""
        sandbox_dir = create_sandbox(prefix="terraagent_tf_")
        results: Dict[str, Any] = {
            "passed": False,
            "checks": [],
            "sandbox_dir": sandbox_dir
        }

        try:
            # Write HCL files to sandbox
            for filename, content in hcl_files.items():
                file_path = os.path.join(sandbox_dir, filename)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

            # 1. Format check
            code, out, err = await cls.run_command([binary, "fmt", "-check"], cwd=sandbox_dir)
            results["checks"].append({
                "check_name": "fmt",
                "passed": code == 0,
                "output": out + ("\n" + err if err else "")
            })

            # 2. Init (backend=false)
            code, out, err = await cls.run_command(
                [binary, "init", "-backend=false"],
                cwd=sandbox_dir
            )
            init_passed = code == 0
            results["checks"].append({
                "check_name": "init",
                "passed": init_passed,
                "output": out + ("\n" + err if err else "")
            })

            # 3. Validate
            if init_passed:
                code, out, err = await cls.run_command(
                    [binary, "validate", "-json"],
                    cwd=sandbox_dir
                )
                val_passed = code == 0
                results["checks"].append({
                    "check_name": "validate",
                    "passed": val_passed,
                    "output": out + ("\n" + err if err else "")
                })
                results["passed"] = val_passed
            else:
                results["passed"] = False

        except Exception as e:
            logger.error(f"Validation execution error: {e}")
            results["checks"].append({
                "check_name": "system",
                "passed": False,
                "output": str(e)
            })
            results["passed"] = False
        finally:
            # Clean up sandbox
            release_sandbox(sandbox_dir)

        return results

    @classmethod
    async def plan_json(
        cls,
        hcl_files: Dict[str, str],
        aws_credentials: Dict[str, Optional[str]],
        region: str,
        binary: str = "terraform",
    ) -> Dict[str, Any]:
        """Run a REAL `<binary> init && plan && show -json` against live AWS and
        tally create/update/replace/destroy/no-op actions from resource_changes.

        This is the one place in this codebase a real AWS provider handshake
        happens outside boto3 - unlike validate_hcl (schema-only, no network, no
        credentials needed), a meaningful plan against import {} blocks needs the
        provider to actually reach AWS. Two things make that safe to do here:

        1. Credentials are scoped to THIS subprocess call only, via an explicit
           env dict - never os.environ.copy(), which would blindly forward
           whatever else happens to be in the parent process's environment.
           Only PATH/HOME/TF_PLUGIN_CACHE_DIR (needed for the binary and its
           provider plugin cache to work at all) plus the AWS credential vars
           are included. TF_LOG/TF_LOG_PATH are explicitly forced unset even if
           somehow present upstream - Terraform provider debug logging can write
           raw request/response bodies, credentials included, to disk.
        2. Every check's stdout/stderr is scrubbed via CredentialScrubber before
           being placed into the returned dict, since that dict is what gets
           persisted into TerraAgentState (and from there, Redis/Postgres) -
           unlike the env dict itself, which is local to this call and never
           stored anywhere.

        `plan`/`show` are already permitted by run_command's disallowed-argv
        check (only apply/destroy/import are blocked) and neither ever mutates
        real infrastructure - `plan` only ever reads, and `-out` just writes a
        local plan file inside the sandbox that's destroyed with it.
        """
        sandbox_dir = create_sandbox(prefix="terraagent_plan_")
        result: Dict[str, Any] = {
            "passed": False,
            "skipped": False,
            "create": 0,
            "update": 0,
            "replace": 0,
            "destroy": 0,
            "no_op": 0,
            "blocking_actions": [],
            "checks": [],
        }

        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "TF_PLUGIN_CACHE_DIR": os.environ.get("TF_PLUGIN_CACHE_DIR", ""),
            "TF_IN_AUTOMATION": "1",
            "AWS_ACCESS_KEY_ID": aws_credentials.get("access_key") or "",
            "AWS_SECRET_ACCESS_KEY": aws_credentials.get("secret_key") or "",
            "AWS_DEFAULT_REGION": region,
        }
        if aws_credentials.get("session_token"):
            env["AWS_SESSION_TOKEN"] = aws_credentials["session_token"]
        # Belt-and-suspenders: guarantee these never end up in the scoped env
        # even if a future edit above ever starts from a broader base dict.
        env.pop("TF_LOG", None)
        env.pop("TF_LOG_PATH", None)

        try:
            for filename, content in hcl_files.items():
                with open(os.path.join(sandbox_dir, filename), "w", encoding="utf-8") as f:
                    f.write(content)

            code, out, err = await cls.run_command(
                [binary, "init", "-backend=false", "-input=false"], cwd=sandbox_dir, env=env
            )
            init_passed = code == 0
            result["checks"].append({
                "check_name": "init",
                "passed": init_passed,
                "output": CredentialScrubber.scrub_text(out + ("\n" + err if err else "")),
            })
            if not init_passed:
                return result

            code, out, err = await cls.run_command(
                [binary, "plan", "-out=tfplan", "-input=false", "-lock=false"], cwd=sandbox_dir, env=env
            )
            plan_passed = code == 0
            result["checks"].append({
                "check_name": "plan",
                "passed": plan_passed,
                "output": CredentialScrubber.scrub_text(out + ("\n" + err if err else "")),
            })
            if not plan_passed:
                return result

            code, out, err = await cls.run_command(
                [binary, "show", "-json", "tfplan"], cwd=sandbox_dir, env=env
            )
            if code != 0:
                result["checks"].append({
                    "check_name": "show",
                    "passed": False,
                    "output": CredentialScrubber.scrub_text(err or out),
                })
                return result

            plan_data = json.loads(out)
            resource_changes = plan_data.get("resource_changes", []) or []
            blocking: List[Dict[str, str]] = []
            for change in resource_changes:
                actions = change.get("change", {}).get("actions", [])
                address = change.get("address", "unknown")
                if actions in (["no-op"], ["read"]):
                    result["no_op"] += 1
                elif actions == ["create"]:
                    result["create"] += 1
                elif actions == ["update"]:
                    result["update"] += 1
                elif "delete" in actions and "create" in actions:
                    result["replace"] += 1
                    blocking.append({"address": address, "action": "replace"})
                elif actions == ["delete"]:
                    result["destroy"] += 1
                    blocking.append({"address": address, "action": "destroy"})

            result["blocking_actions"] = blocking
            result["passed"] = not blocking
            result["checks"].append({
                "check_name": "show",
                "passed": True,
                "output": f"{len(resource_changes)} resource_changes parsed from plan output.",
            })
        except Exception as e:
            result["checks"].append({
                "check_name": "system",
                "passed": False,
                "output": CredentialScrubber.scrub_text(str(e)),
            })
        finally:
            release_sandbox(sandbox_dir)

        return result
