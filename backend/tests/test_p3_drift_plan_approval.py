"""Comprehensive test suite for Phase 3 (P3):
Drift Reconciliation + Plan Equivalence + Approval Safety.
"""

from unittest.mock import AsyncMock, patch
import pytest

from agents.drift_reconciliation_agent import (
    _diff_scalar_fields,
    _diff_security_group_rules,
    _diff_tags,
    drift_reconciliation_agent_node,
)
from agents.plan_equivalence_agent import plan_equivalence_agent_node
from tools.terraform_runner import TerraformRunner


def _base_state(**overrides):
    state = {
        "job_id": "test-p3-job",
        "region": "us-east-1",
        "resources": [],
        "classification_results": {"classifications": []},
        "adoption_plan": {"categories": []},
        "terraform_files": {"main.tf": ""},
        "terraform_binary": "terraform",
        "run_plan_equivalence": True,
        "aws_credentials": {"access_key": "AKIAEXAMPLEP3KEY", "secret_key": "secretsecretsecretsecretsecret"},
        "aws_endpoint_url": None,
        "completed_agents": [],
        "pending_approval": None,
        "repair_risk_tier": None,
    }
    state.update(overrides)
    return state


# =========================================================================
# 1. Critical P3 Regression Test: Intentional VPC CIDR Mismatch
# =========================================================================

@pytest.mark.asyncio
async def test_critical_vpc_cidr_mismatch_triggers_destructive_equivalent():
    """Live VPC has 10.1.0.0/16, but generated HCL has 10.0.0.0/16.
    Must be classified as destructive_equivalent and halt for human approval."""
    discovered = {
        "id": "vpc-test-mismatch",
        "resource_type": "aws_vpc",
        "name": "prod-vpc",
        "cidr_block": "10.0.0.0/16",
        "tags": []
    }
    live_vpc = {
        "id": "vpc-test-mismatch",
        "resource_type": "aws_vpc",
        "name": "prod-vpc",
        "cidr_block": "10.1.0.0/16",
        "tags": []
    }

    state = _base_state(
        resources=[discovered],
        classification_results={
            "classifications": [{"resource_id": "vpc-test-mismatch", "recommended_action": "import"}]
        },
        adoption_plan={
            "categories": [{"category": "safe_to_import", "resource_ids": ["vpc-test-mismatch"]}]
        }
    )

    with patch("agents.drift_reconciliation_agent.fetch_live_resource", return_value=live_vpc):
        result = await drift_reconciliation_agent_node(state)

    # 1. Verification of drift results
    drift_res = result["drift_results"]
    assert drift_res["skipped"] is False
    assert drift_res["destructive_equivalent_count"] == 1
    assert drift_res["behavior_changing_count"] == 0
    assert drift_res["resources_checked"] == 1

    # 2. Verification of findings
    finding = drift_res["findings"][0]
    assert finding["attribute"] == "cidr_block"
    assert finding["live_value"] == "10.1.0.0/16"
    assert finding["generated_value"] == "10.0.0.0/16"
    assert finding["tier"] == "destructive_equivalent"
    assert finding["impact"] == "replacement"

    # 3. Verification of Approval Gate Halt
    assert result["current_agent"] == "awaiting_approval"
    assert result["status"] == "AWAITING_APPROVAL"
    assert result["pending_approval"]["reason"] == "drift_reconciliation_requires_human_approval"
    assert result["repair_risk_tier"] == "destructive"


# =========================================================================
# 2. Severity Classification Tests
# =========================================================================

def test_drift_scalar_fields_forcenew_vs_non_forcenew():
    """Verify ForceNew fields produce destructive_equivalent and non-ForceNew produce behavior_changing."""
    # Subnet CIDR (ForceNew: True)
    sub_disc = {"cidr_block": "10.0.1.0/24", "vpc_id": "vpc-1"}
    sub_live = {"cidr_block": "10.0.2.0/24", "vpc_id": "vpc-1"}
    sub_findings = _diff_scalar_fields("aws_subnet", "subnet-1", sub_disc, sub_live)
    assert len(sub_findings) == 1
    assert sub_findings[0]["tier"] == "destructive_equivalent"
    assert sub_findings[0]["impact"] == "replacement"

    # EC2 Instance Type (ForceNew: False)
    ec2_disc = {"instance_type": "t3.micro", "ami": "ami-123"}
    ec2_live = {"instance_type": "t3.large", "ami": "ami-123"}
    ec2_findings = _diff_scalar_fields("aws_instance", "i-1", ec2_disc, ec2_live)
    assert len(ec2_findings) == 1
    assert ec2_findings[0]["tier"] == "behavior_changing"
    assert ec2_findings[0]["impact"] == "configuration_mismatch"


def test_drift_tags_always_informational():
    """Tag differences are purely cosmetic and should always be classified as informational."""
    disc = {"tags": [{"Key": "Env", "Value": "Dev"}]}
    live = {"tags": [{"Key": "Env", "Value": "Prod"}]}
    findings = _diff_tags("aws_vpc", "vpc-1", disc, live)
    assert len(findings) == 1
    assert findings[0]["tier"] == "informational"
    assert findings[0]["impact"] == "cosmetic_metadata"


def test_security_group_rule_diffs():
    """Security Group rule changes produce rule-level behavior_changing findings."""
    disc = {
        "ip_permissions": [
            {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
        ],
        "ip_permissions_egress": []
    }
    live = {
        "ip_permissions": [
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
        ],
        "ip_permissions_egress": []
    }
    findings = _diff_security_group_rules("sg-1", disc, live)
    assert len(findings) == 2
    assert all(f["tier"] == "behavior_changing" for f in findings)


# =========================================================================
# 3. Empty Environment and Skip Handling
# =========================================================================

@pytest.mark.asyncio
async def test_drift_skips_when_no_adopted_resources():
    """If no resources are marked for adoption, drift check is skipped without halting."""
    state = _base_state(
        resources=[{"id": "res-unmanaged", "resource_type": "aws_vpc"}],
        classification_results={"classifications": [{"resource_id": "res-unmanaged", "recommended_action": "skip"}]}
    )
    result = await drift_reconciliation_agent_node(state)
    assert result["drift_results"]["skipped"] is True
    assert result["current_agent"] == "plan_equivalence_agent"
    assert result.get("pending_approval") is None


# =========================================================================
# 4. Plan Equivalence JSON Parsing & Risk Classification
# =========================================================================

@pytest.mark.asyncio
async def test_plan_equivalence_clean_plan_continues():
    """A clean plan with only create or no_op continues to policy_agent."""
    clean_plan = {
        "passed": True,
        "create": 3,
        "update": 0,
        "replace": 0,
        "destroy": 0,
        "no_op": 1,
        "blocking_actions": [],
        "checks": [{"check_name": "plan", "passed": True, "output": "Plan: 3 to add, 0 to change, 0 to destroy."}]
    }
    state = _base_state()

    with patch("tools.terraform_runner.TerraformRunner.plan_json", new=AsyncMock(return_value=clean_plan)):
        result = await plan_equivalence_agent_node(state)

    assert result["current_agent"] == "policy_agent"
    assert result.get("pending_approval") is None
    assert result["plan_equivalence_results"]["create"] == 3


@pytest.mark.asyncio
async def test_plan_equivalence_replace_or_destroy_halts_for_approval():
    """Plan reporting a replace or destroy action sets pending_approval and halts."""
    blocking_plan = {
        "passed": False,
        "create": 1,
        "update": 0,
        "replace": 1,
        "destroy": 1,
        "no_op": 0,
        "blocking_actions": [
            {"address": "aws_instance.web", "action": "replace"},
            {"address": "aws_security_group.old", "action": "destroy"}
        ],
        "checks": []
    }
    state = _base_state()

    with patch("tools.terraform_runner.TerraformRunner.plan_json", new=AsyncMock(return_value=blocking_plan)):
        result = await plan_equivalence_agent_node(state)

    assert result["current_agent"] == "awaiting_approval"
    assert result["status"] == "AWAITING_APPROVAL"
    assert result["pending_approval"]["reason"] == "plan_equivalence_requires_human_approval"
    assert result["repair_risk_tier"] == "destructive"
    assert len(result["pending_approval"]["findings"]) == 2


# =========================================================================
# 5. Safety: Apply / Destroy / Import Remain Hard-Blocked
# =========================================================================

@pytest.mark.asyncio
async def test_plan_runner_prohibits_mutating_commands():
    """Ensure TerraformRunner rejects apply, destroy, and import commands."""
    with pytest.raises(ValueError, match="Safety Violation"):
        await TerraformRunner.run_command(["terraform", "apply"], cwd="/tmp")

    with pytest.raises(ValueError, match="Safety Violation"):
        await TerraformRunner.run_command(["tofu", "destroy"], cwd="/tmp")

    with pytest.raises(ValueError, match="Safety Violation"):
        await TerraformRunner.run_command(["terraform", "import", "aws_vpc.main", "vpc-123"], cwd="/tmp")
