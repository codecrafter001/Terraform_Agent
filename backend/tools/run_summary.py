"""A compact, durable record of the terraform/tofu commands a job ran, for the
Terraform Runs page. Built from the final graph state when a job finishes or
pauses (services/database.py::mark_job_complete) and stored on the audit
record, because the full state in Redis expires after a day.

Only command names, pass/fail, counts and timings are kept - never command
output (it can be long, and the plan output names live resources) and never
anything credential-related. Every value here was produced by
TerraformRunner.run_command, so the commands themselves were only ever the
allow-listed read-only subcommands (fmt/init/validate/plan/show).
"""

from typing import Any, Dict, List, Optional

# Which runner call produced a check, and the command it stands for.
_STAGES = (
    ("validation", "validation_results", "validation_agent"),
    ("plan", "plan_equivalence_results", "plan_equivalence_agent"),
    ("generate_config", "config_crosscheck", "config_crosscheck"),
)

_COMMANDS: Dict[str, str] = {
    "fmt": "fmt -check",
    "init": "init -backend=false",
    "validate": "validate -json",
    "plan": "plan -out=tfplan (import blocks, read-only)",
    "show": "show -json tfplan",
    "generate_config": "plan -generate-config-out",
    "system": "(runner error)",
}

_ITERATION_KEYS = ("iteration", "verdict", "validation_passed", "plan_changes", "imported",
                   "config_mismatches", "total_findings", "passed", "at")


def _int(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def build_runs_summary(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """None when the job never reached a terraform command (e.g. it failed in discovery)."""
    timings = state.get("agent_timings") or {}
    stages: List[Dict[str, Any]] = []
    for stage, key, step in _STAGES:
        result = state.get(key) or {}
        checks = [c for c in (result.get("checks") or []) if isinstance(c, dict) and c.get("check_name")]
        entry: Dict[str, Any] = {
            "stage": stage,
            "skipped": bool(result.get("skipped")) if result else True,
            "reason": str(result.get("reason") or "")[:200] or None,
            "seconds": round(float(timings[step]), 1) if isinstance(timings.get(step), (int, float)) else None,
            "commands": [
                {
                    "name": str(c["check_name"]),
                    "command": _COMMANDS.get(str(c["check_name"]), str(c["check_name"])),
                    "passed": bool(c.get("passed")),
                }
                for c in checks
            ],
        }
        if stage == "plan" and result and not result.get("skipped"):
            entry["counts"] = {k: _int(result.get(k)) for k in ("create", "update", "replace", "destroy", "imported")}
        if stage == "generate_config" and result and not result.get("skipped") and "mismatches" in result:
            entry["mismatches"] = len(result.get("mismatches") or [])
        stages.append(entry)

    iterations = [
        {k: it.get(k) for k in _ITERATION_KEYS if k in it}
        for it in (state.get("verification_iterations") or []) if isinstance(it, dict)
    ]
    if not iterations and not any(s["commands"] for s in stages):
        return None
    return {
        "engine": state.get("terraform_binary") or "terraform",
        "verdict": state.get("verification_verdict"),
        "repair_attempts": _int(state.get("repair_attempts")) or 0,
        "iterations": iterations,
        "stages": stages,
    }
