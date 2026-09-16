"""Unit tests for plan_equivalence_agent_node - the skip paths (opt-in flag
off, no credentials) and the blocking path (a replace/destroy action halts
the pipeline for human approval). TerraformRunner.plan_json itself is
covered by test_terraform_runner_plan.py; here it's mocked so these tests
don't need a real Terraform binary."""

from unittest.mock import AsyncMock, patch

import pytest

from agents.plan_equivalence_agent import plan_equivalence_agent_node


def _base_state(**overrides):
    state = {
        "job_id": "test",
        "terraform_files": {"application.tf": ""},
        "region": "us-east-1",
        "terraform_binary": "terraform",
        "run_plan_equivalence": True,
        "aws_credentials": {"access_key": "AKIAFAKE", "secret_key": "fakefakefakefakefakefakefakefakefakefake"},
        "completed_agents": [],
        "pending_approval": None,
        "repair_risk_tier": None,
    }
    state.update(overrides)
    return state


@pytest.mark.asyncio
async def test_skips_when_run_plan_equivalence_is_false():
    state = _base_state(run_plan_equivalence=False)
    with patch("agents.plan_equivalence_agent.TerraformRunner.plan_json", new=AsyncMock()) as mocked:
        result = await plan_equivalence_agent_node(state)

    mocked.assert_not_called()
    assert result["plan_equivalence_results"]["skipped"] is True
    assert result["current_agent"] == "policy_agent"
    assert "pending_approval" not in result or result.get("pending_approval") is None


@pytest.mark.asyncio
async def test_skips_when_no_aws_credentials_available():
    state = _base_state(aws_credentials={})
    with patch("agents.plan_equivalence_agent.TerraformRunner.plan_json", new=AsyncMock()) as mocked:
        result = await plan_equivalence_agent_node(state)

    mocked.assert_not_called()
    assert result["plan_equivalence_results"]["skipped"] is True
    assert result["current_agent"] == "policy_agent"


@pytest.mark.asyncio
async def test_clean_plan_continues_to_policy_agent():
    clean_result = {
        "passed": True, "create": 2, "update": 0, "replace": 0, "destroy": 0,
        "no_op": 1, "blocking_actions": [], "checks": [], "skipped": False,
    }
    state = _base_state()
    with patch("agents.plan_equivalence_agent.TerraformRunner.plan_json", new=AsyncMock(return_value=clean_result)):
        result = await plan_equivalence_agent_node(state)

    assert result["plan_equivalence_results"] == clean_result
    assert result["current_agent"] == "policy_agent"
    assert result.get("pending_approval") is None
    assert "status" not in result


@pytest.mark.asyncio
async def test_blocking_plan_halts_pipeline_for_approval():
    blocking_result = {
        "passed": False, "create": 1, "update": 0, "replace": 1, "destroy": 0,
        "no_op": 0, "blocking_actions": [{"address": "aws_db_instance.mydb", "action": "replace"}],
        "checks": [], "skipped": False,
    }
    state = _base_state()
    with patch("agents.plan_equivalence_agent.TerraformRunner.plan_json", new=AsyncMock(return_value=blocking_result)):
        result = await plan_equivalence_agent_node(state)

    assert result["current_agent"] == "awaiting_approval"
    assert result["status"] == "AWAITING_APPROVAL"
    assert result["pending_approval"]["reason"] == "plan_equivalence_requires_human_approval"
    finding = result["pending_approval"]["findings"][0]
    assert finding["resource"] == "aws_db_instance.mydb"
    assert finding["tier"] == "behavior_changing"  # replace, not destroy
    assert result["repair_risk_tier"] == "behavior_changing"


@pytest.mark.asyncio
async def test_destroy_action_classified_as_destructive_tier():
    blocking_result = {
        "passed": False, "create": 0, "update": 0, "replace": 0, "destroy": 1,
        "no_op": 0, "blocking_actions": [{"address": "aws_iam_role.orphan", "action": "destroy"}],
        "checks": [], "skipped": False,
    }
    state = _base_state()
    with patch("agents.plan_equivalence_agent.TerraformRunner.plan_json", new=AsyncMock(return_value=blocking_result)):
        result = await plan_equivalence_agent_node(state)

    assert result["pending_approval"]["findings"][0]["tier"] == "destructive"
    assert result["repair_risk_tier"] == "destructive"


@pytest.mark.asyncio
async def test_blocking_plan_preserves_existing_pending_approval_findings():
    # A prior run of this same node (e.g. re-entering via the repair loop)
    # could already have set pending_approval - a fresh blocking plan must
    # extend that list, not overwrite it.
    existing_finding = {
        "tool": "tfsec", "rule_id": "AVD-AWS-0080", "severity": "HIGH",
        "description": "storage not encrypted", "resource": "aws_db_instance.other", "tier": "destructive",
    }
    blocking_result = {
        "passed": False, "create": 0, "update": 0, "replace": 1, "destroy": 0,
        "no_op": 0, "blocking_actions": [{"address": "aws_vpc.main", "action": "replace"}],
        "checks": [], "skipped": False,
    }
    state = _base_state(
        pending_approval={"reason": "repair_requires_human_approval", "findings": [existing_finding]},
        repair_risk_tier="destructive",
    )
    with patch("agents.plan_equivalence_agent.TerraformRunner.plan_json", new=AsyncMock(return_value=blocking_result)):
        result = await plan_equivalence_agent_node(state)

    findings = result["pending_approval"]["findings"]
    assert len(findings) == 2
    assert existing_finding in findings
    assert result["repair_risk_tier"] == "destructive"  # existing destructive outranks new behavior_changing


@pytest.mark.asyncio
async def test_init_failure_skips_rather_than_escalates():
    # The exact same files already passed terraform init in validation_agent
    # moments earlier - a fresh init failure here is almost certainly a
    # transient environment/registry issue, not something a human approval
    # decision could fix. Must NOT halt the pipeline for this.
    init_failed_result = {
        "passed": False, "create": 0, "update": 0, "replace": 0, "destroy": 0, "no_op": 0,
        "blocking_actions": [],
        "checks": [{"check_name": "init", "passed": False, "output": "connection reset by registry.terraform.io"}],
        "skipped": False,
    }
    state = _base_state()
    with patch("agents.plan_equivalence_agent.TerraformRunner.plan_json", new=AsyncMock(return_value=init_failed_result)):
        result = await plan_equivalence_agent_node(state)

    assert result["current_agent"] == "policy_agent"
    assert result.get("pending_approval") is None
    assert "status" not in result


@pytest.mark.asyncio
async def test_plan_failure_against_real_aws_is_blocking():
    # This is the actually-reachable, meaningful signal today: init succeeds
    # (the sandbox and provider are fine) but a real AWS API call rejects
    # something in the configuration - terraform validate could never catch
    # this since it never talks to AWS at all.
    plan_failed_result = {
        "passed": False, "create": 0, "update": 0, "replace": 0, "destroy": 0, "no_op": 0,
        "blocking_actions": [],
        "checks": [
            {"check_name": "init", "passed": True, "output": ""},
            {"check_name": "plan", "passed": False, "output": "InvalidParameterValue: subnet subnet-0000 does not exist"},
        ],
        "skipped": False,
    }
    state = _base_state()
    with patch("agents.plan_equivalence_agent.TerraformRunner.plan_json", new=AsyncMock(return_value=plan_failed_result)):
        result = await plan_equivalence_agent_node(state)

    assert result["current_agent"] == "awaiting_approval"
    assert result["status"] == "AWAITING_APPROVAL"
    finding = result["pending_approval"]["findings"][0]
    assert finding["rule_id"] == "plan-equivalence-error"
    assert finding["tier"] == "destructive"
    assert "subnet-0000" in finding["description"]
    assert result["repair_risk_tier"] == "destructive"


@pytest.mark.asyncio
async def test_skip_and_clean_paths_explicitly_preserve_existing_pending_approval():
    # Regression guard: every return path must explicitly carry
    # pending_approval/repair_risk_tier forward (even if just re-stating the
    # existing value), never omit the keys - LangGraph merges a node's
    # return via {**old_state, **result}, so an omitted key would silently
    # keep a stale value if this node ever re-enters with one already set.
    existing = {"reason": "repair_requires_human_approval", "findings": [{"resource": "x"}]}

    state = _base_state(run_plan_equivalence=False, pending_approval=existing, repair_risk_tier="destructive")
    result = await plan_equivalence_agent_node(state)
    assert result["pending_approval"] == existing
    assert result["repair_risk_tier"] == "destructive"

    clean_result = {
        "passed": True, "create": 1, "update": 0, "replace": 0, "destroy": 0,
        "no_op": 0, "blocking_actions": [], "checks": [], "skipped": False,
    }
    state = _base_state(pending_approval=existing, repair_risk_tier="destructive")
    with patch("agents.plan_equivalence_agent.TerraformRunner.plan_json", new=AsyncMock(return_value=clean_result)):
        result = await plan_equivalence_agent_node(state)
    assert result["pending_approval"] == existing
    assert result["repair_risk_tier"] == "destructive"
