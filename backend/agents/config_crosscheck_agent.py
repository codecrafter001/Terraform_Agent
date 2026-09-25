"""Verification step: cross-check generated attributes against Terraform's
own `plan -generate-config-out` output (tools/config_crosscheck.py).

Opt-in with plan equivalence (it needs a real, read-only provider handshake
with AWS credentials) and only meaningful when imports.tf exists. Report only:
mismatches are generator attribute mistakes for a human to see, never
auto-fixed. Fails closed - if it was supposed to run and couldn't, the
verifier treats the pass as INCOMPLETE.
"""

from typing import Any, Dict

from services.redis_client import redis_service
from tools.config_crosscheck import compare
from tools.import_blocks import IMPORTS_FILENAME, import_targets
from tools.terraform_runner import TerraformRunner


async def _log(job_id: str, message: str) -> None:
    await redis_service.publish_log(job_id, f"[AGENT:config_crosscheck] {message}", agent_name="config_crosscheck")


async def config_crosscheck_node(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "")
    files: Dict[str, str] = state.get("terraform_files") or {}
    creds = state.get("aws_credentials") or {}
    completed = list(state.get("completed_agents", []) or [])
    if "config_crosscheck" not in completed:
        completed.append("config_crosscheck")

    def done(result: Dict[str, Any]) -> Dict[str, Any]:
        return {"config_crosscheck": result, "completed_agents": completed, "current_agent": "policy_agent"}

    if not state.get("run_plan_equivalence"):
        return done({"skipped": True, "reason": "runs with plan equivalence (opt-in)"})
    if IMPORTS_FILENAME not in files:
        return done({"skipped": True, "reason": "no import blocks to cross-check"})
    if not (creds.get("access_key") and creds.get("secret_key")):
        return done({"skipped": True, "reason": "no AWS credentials available"})

    await _log(job_id, "Cross-checking generated attributes against `terraform plan -generate-config-out`...")
    run = await TerraformRunner.generate_config(
        files, creds, state.get("region", "us-east-1"), state.get("terraform_binary", "terraform")
    )
    if not run.get("passed"):
        await _log(job_id, "Terraform did not produce generated config - cross-check could not run.")
        return done({"skipped": False, "error": "generate-config-out produced no output", "checks": run.get("checks", [])})

    targets = sorted(import_targets(files[IMPORTS_FILENAME]))
    result = compare(files, run.get("generated", ""), targets)
    n = len(result["mismatches"])
    await _log(
        job_id,
        f"Checked {result['resources_checked']} resources: "
        + (f"{n} attribute mismatch{'es' if n != 1 else ''} with live AWS." if n else "all literal attributes match live AWS."),
    )
    return done({"skipped": False, **result, "checks": run.get("checks", [])})
