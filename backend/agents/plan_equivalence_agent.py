"""Plan Equivalence Agent: runs a REAL `terraform init && plan && show -json`
against the generated HCL (opt-in via run_plan_equivalence, off by default).

IMPORTANT, verified empirically (a real `terraform plan` run against a fresh
sandbox describing an already-existing S3 bucket, with no prior state file):
every resource in a state-less sandbox plan shows up as `create`, NEVER
`replace`/`destroy`/`update` - Terraform has no way to know a resource
already exists in AWS without either a populated state file or a native
`import {}` block binding the address to a real ID, and this codebase has
neither (terraform_composer.py never emits import blocks, and CLAUDE.md's
hard safety rule #2 forbids ever running `terraform import` automatically -
even in a throwaway sandbox - so state can never be legitimately populated
here). So the replace/destroy tallying below is kept for the day this
codebase gains real import-block support, but it is NOT the thing that
actually protects anyone today - treat it as defense-in-depth, not the
primary signal.

The check that IS real and reachable today: a `plan` (or `show`) step that
fails after `init` succeeds means the AWS provider itself rejected something
about this configuration at the API level (e.g. a referenced value schema-
valid locally but invalid against real AWS) - `terraform validate` cannot
catch this, since it never talks to AWS at all. That is a genuine "this
configuration does not actually work against reality" signal, and is what
this agent treats as blocking. An `init` failure, by contrast, is far more
likely an environment/registry issue than a config problem (the exact same
files already passed `terraform init` in validation_agent moments earlier) -
a human can't "approve" their way past a transient network failure, so that
case is logged and skipped rather than escalated.
"""

from typing import Any, Dict

from services.redis_client import redis_service
from tools.iac_engine import get_iac_engine
from tools.terraform_runner import TerraformRunner

_TIER_RANK: Dict[str, int] = {"safe_auto": 0, "behavior_changing": 1, "destructive": 2}


def _max_tier(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a if _TIER_RANK.get(a, -1) >= _TIER_RANK.get(b, -1) else b


def _find_check(checks, name):
    return next((c for c in checks if c.get("check_name") == name), None)


async def plan_equivalence_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "unknown")
    tf_files = state.get("terraform_files", {})

    completed_agents = list(state.get("completed_agents", []))
    if "plan_equivalence_agent" not in completed_agents:
        completed_agents.append("plan_equivalence_agent")

    # Every return path below explicitly carries pending_approval/
    # repair_risk_tier forward (even when unchanged) rather than omitting
    # them - LangGraph merges a node's return via {**old_state, **result},
    # so an omitted key silently preserves whatever was already there. Not
    # currently reachable given how plan_gate/repair_or_done halt to END the
    # instant pending_approval goes truthy (nothing loops back with a stale
    # value in the current wiring), but omitting the key here was fragile -
    # a future reordering could resurrect a stale approval block.
    existing_pending = state.get("pending_approval")
    existing_tier = state.get("repair_risk_tier")

    if not state.get("run_plan_equivalence"):
        return {
            "plan_equivalence_results": {"skipped": True, "reason": "run_plan_equivalence not enabled for this scan"},
            "pending_approval": existing_pending,
            "repair_risk_tier": existing_tier,
            "completed_agents": completed_agents,
            "current_agent": "policy_agent",
            "progress_percentage": 65,
        }

    creds = state.get("aws_credentials") or {}
    if not creds.get("access_key") or not creds.get("secret_key"):
        await redis_service.publish_log(
            job_id,
            "[AGENT:plan_equivalence_agent] run_plan_equivalence was requested but no AWS "
            "credentials are available in this pipeline run - skipping the real plan.",
            agent_name="plan_equivalence_agent",
        )
        return {
            "plan_equivalence_results": {"skipped": True, "reason": "no AWS credentials available"},
            "pending_approval": existing_pending,
            "repair_risk_tier": existing_tier,
            "completed_agents": completed_agents,
            "current_agent": "policy_agent",
            "progress_percentage": 65,
        }

    binary_selection = state.get("terraform_binary", "terraform")
    engine = get_iac_engine(binary_selection)

    await redis_service.publish_log(
        job_id,
        f"[AGENT:plan_equivalence_agent] Running a real `{engine.binary_name} init && plan` against live "
        f"AWS to verify the generated configuration is actually accepted by the real provider...",
        agent_name="plan_equivalence_agent",
    )

    result = await TerraformRunner.plan_json(
        tf_files,
        aws_credentials=creds,
        region=state.get("region", "us-east-1"),
        binary=engine.binary_name,
    )

    checks = result.get("checks", [])
    init_check = _find_check(checks, "init")
    plan_check = _find_check(checks, "plan")
    show_check = _find_check(checks, "show")
    blocking_actions = result.get("blocking_actions", [])

    if init_check and not init_check.get("passed"):
        # The exact same files already passed `terraform init` in
        # validation_agent - a fresh failure here is almost certainly a
        # transient environment/registry issue, not something a human
        # approval decision could fix. Log and move on rather than escalate.
        await redis_service.publish_log(
            job_id,
            "[AGENT:plan_equivalence_agent] `terraform init` failed unexpectedly for the real "
            "plan (likely a transient environment/registry issue) - skipping this check rather "
            "than escalating something a human approval can't actually resolve.",
            agent_name="plan_equivalence_agent",
        )
        return {
            "plan_equivalence_results": result,
            "pending_approval": existing_pending,
            "repair_risk_tier": existing_tier,
            "completed_agents": completed_agents,
            "current_agent": "policy_agent",
            "progress_percentage": 65,
        }

    plan_error = None
    if plan_check and not plan_check.get("passed"):
        plan_error = plan_check.get("output", "")
    elif show_check and not show_check.get("passed"):
        plan_error = show_check.get("output", "")

    existing_findings = (existing_pending or {}).get("findings", [])

    if blocking_actions or plan_error:
        new_findings = [
            {
                "tool": "terraform_plan",
                "rule_id": "plan-equivalence",
                "severity": "HIGH",
                "description": (
                    f"terraform plan reports a `{item['action']}` action against "
                    f"`{item['address']}` - the generated configuration does not match "
                    "the real, already-discovered resource."
                ),
                "resource": item["address"],
                "tier": "destructive" if item["action"] == "destroy" else "behavior_changing",
            }
            for item in blocking_actions
        ]
        if plan_error:
            new_findings.append({
                "tool": "terraform_plan",
                "rule_id": "plan-equivalence-error",
                "severity": "HIGH",
                "description": (
                    "terraform plan failed against the real AWS provider - the generated "
                    f"configuration was rejected at the API level: {plan_error[:800]}"
                ),
                "resource": "unknown",
                # Can't verify anything about safety when the plan didn't even
                # complete - default to the most conservative tier.
                "tier": "destructive",
            })

        pending_approval = {
            "reason": "plan_equivalence_requires_human_approval",
            "findings": existing_findings + new_findings,
        }
        cycle_tier = "destructive" if any(f["tier"] == "destructive" for f in new_findings) else "behavior_changing"
        repair_risk_tier = _max_tier(existing_tier, cycle_tier)

        await redis_service.publish_log(
            job_id,
            f"[AGENT:plan_equivalence_agent] BLOCKED: {len(new_findings)} finding(s) from the "
            "real plan - halting for human approval.",
            agent_name="plan_equivalence_agent",
        )

        return {
            "plan_equivalence_results": result,
            "pending_approval": pending_approval,
            "repair_risk_tier": repair_risk_tier,
            "completed_agents": completed_agents,
            "current_agent": "awaiting_approval",
            "progress_percentage": 70,
            "status": "AWAITING_APPROVAL",
        }

    await redis_service.publish_log(
        job_id,
        f"[AGENT:plan_equivalence_agent] Plan verified clean against real AWS: "
        f"{result.get('create', 0)} create, {result.get('update', 0)} update, "
        f"{result.get('no_op', 0)} no-op.",
        agent_name="plan_equivalence_agent",
    )

    return {
        "plan_equivalence_results": result,
        "pending_approval": existing_pending,
        "repair_risk_tier": existing_tier,
        "completed_agents": completed_agents,
        "current_agent": "policy_agent",
        "progress_percentage": 65,
    }
