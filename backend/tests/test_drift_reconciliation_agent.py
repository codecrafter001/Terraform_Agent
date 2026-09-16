"""Unit tests for drift_reconciliation_agent.py - the diff functions in
isolation, the node's skip/clean/blocking paths, tier-vocabulary mapping
onto the shared pending_approval shape, and (most importantly) a regression
guard that runs the REAL terraform_composer_node and this agent back-to-back
on the same resource and asserts zero drift - this agent's _FIELD_MAP table
mirrors terraform_composer.py's own template defaults by hand (not a shared
constant, to avoid touching proven composer code for this), so nothing else
catches the two silently diverging except this test.
"""

from unittest.mock import patch

import pytest

from agents.drift_reconciliation_agent import (
    _diff_scalar_fields,
    _diff_security_group_rules,
    _diff_tags,
    drift_reconciliation_agent_node,
)


def _classification(resource_id, action):
    return {"resource_id": resource_id, "resource_type": "x", "category": "unmanaged", "reason": [], "recommended_action": action}


def _base_state(**overrides):
    state = {
        "job_id": "test",
        "region": "us-east-1",
        "resources": [],
        "classification_results": {"classifications": []},
        "aws_credentials": {"access_key": "AKIAFAKE", "secret_key": "fakefakefakefakefakefakefakefakefakefake"},
        "aws_endpoint_url": None,
        "completed_agents": [],
        "pending_approval": None,
        "repair_risk_tier": None,
    }
    state.update(overrides)
    return state


# --- Pure diff function unit tests -----------------------------------------

def test_diff_scalar_fields_flags_forcenew_mismatch_as_destructive_equivalent():
    discovered = {"cidr_block": "10.0.0.0/16"}
    live = {"cidr_block": "10.5.0.0/16"}
    findings = _diff_scalar_fields("aws_vpc", "vpc-1", discovered, live)
    assert len(findings) == 1
    assert findings[0]["tier"] == "destructive_equivalent"
    assert findings[0]["attribute"] == "cidr_block"


def test_diff_scalar_fields_flags_non_forcenew_mismatch_as_behavior_changing():
    discovered = {"instance_type": "t3.micro", "ami": "ami-123"}
    live = {"instance_type": "t3.large", "ami": "ami-123"}
    findings = _diff_scalar_fields("aws_instance", "i-1", discovered, live)
    assert len(findings) == 1
    assert findings[0]["tier"] == "behavior_changing"
    assert findings[0]["attribute"] == "instance_type"


def test_diff_scalar_fields_default_substitution_is_detected():
    # discovered resource is MISSING cidr_block entirely - the composer's
    # template would have silently substituted its hardcoded default
    # ("10.0.0.0/16") into the HCL. If the real live value differs from
    # that default, this must be caught, not silently ignored.
    discovered = {}  # no cidr_block key at all
    live = {"cidr_block": "192.168.0.0/16"}
    findings = _diff_scalar_fields("aws_vpc", "vpc-1", discovered, live)
    assert len(findings) == 1
    assert findings[0]["generated_value"] == "10.0.0.0/16"  # the composer's own hardcoded default
    assert findings[0]["tier"] == "destructive_equivalent"


def test_diff_scalar_fields_no_mismatch_no_findings():
    discovered = {"cidr_block": "10.0.0.0/16"}
    live = {"cidr_block": "10.0.0.0/16"}
    assert _diff_scalar_fields("aws_vpc", "vpc-1", discovered, live) == []


def test_diff_tags_is_always_informational():
    discovered = {"tags": [{"Key": "Owner", "Value": "team-a"}]}
    live = {"tags": [{"Key": "Owner", "Value": "team-b"}]}
    findings = _diff_tags("aws_vpc", "vpc-1", discovered, live)
    assert len(findings) == 1
    assert findings[0]["tier"] == "informational"


def test_diff_security_group_rules_flags_missing_and_extra_rules():
    discovered = {
        "ip_permissions": [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "10.0.0.0/8"}]}],
        "ip_permissions_egress": [],
    }
    live = {
        "ip_permissions": [{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
        "ip_permissions_egress": [],
    }
    findings = _diff_security_group_rules("sg-1", discovered, live)
    # One rule only in generated HCL (port 22), one rule only live (port 443)
    assert len(findings) == 2
    assert all(f["tier"] == "behavior_changing" for f in findings)
    assert all("rule" in f["attribute"] for f in findings)


# --- Node-level tests --------------------------------------------------------

@pytest.mark.asyncio
async def test_skips_when_no_adopted_resources():
    state = _base_state()
    result = await drift_reconciliation_agent_node(state)
    assert result["drift_results"]["skipped"] is True
    assert result["current_agent"] == "plan_equivalence_agent"
    assert result.get("pending_approval") is None


@pytest.mark.asyncio
async def test_skips_when_no_aws_credentials():
    state = _base_state(
        resources=[{"id": "vpc-1", "resource_type": "aws_vpc", "cidr_block": "10.0.0.0/16"}],
        classification_results={"classifications": [_classification("vpc-1", "import")]},
        aws_credentials={},
    )
    result = await drift_reconciliation_agent_node(state)
    assert result["drift_results"]["skipped"] is True
    assert result["drift_results"]["reason"] == "no AWS credentials available"


@pytest.mark.asyncio
async def test_clean_run_continues_to_plan_equivalence_agent():
    discovered = {"id": "vpc-1", "resource_type": "aws_vpc", "cidr_block": "10.0.0.0/16", "tags": []}
    live = {"cidr_block": "10.0.0.0/16", "tags": []}
    state = _base_state(
        resources=[discovered],
        classification_results={"classifications": [_classification("vpc-1", "import")]},
    )
    with patch("agents.drift_reconciliation_agent.fetch_live_resource", return_value=live):
        result = await drift_reconciliation_agent_node(state)

    assert result["drift_results"]["skipped"] is False
    assert result["drift_results"]["destructive_equivalent_count"] == 0
    assert result["drift_results"]["behavior_changing_count"] == 0
    assert result["current_agent"] == "plan_equivalence_agent"
    assert result.get("pending_approval") is None
    assert "status" not in result


@pytest.mark.asyncio
async def test_missing_resource_is_destructive_equivalent_and_halts():
    discovered = {"id": "vpc-1", "resource_type": "aws_vpc", "cidr_block": "10.0.0.0/16", "tags": []}
    state = _base_state(
        resources=[discovered],
        classification_results={"classifications": [_classification("vpc-1", "import")]},
    )
    with patch("agents.drift_reconciliation_agent.fetch_live_resource", return_value=None):
        result = await drift_reconciliation_agent_node(state)

    assert result["current_agent"] == "awaiting_approval"
    assert result["status"] == "AWAITING_APPROVAL"
    assert result["pending_approval"]["reason"] == "drift_reconciliation_requires_human_approval"
    finding = result["pending_approval"]["findings"][0]
    assert finding["tier"] == "destructive"  # mapped from internal "destructive_equivalent"
    assert finding["tool"] == "drift_reconciliation"
    assert result["repair_risk_tier"] == "destructive"


@pytest.mark.asyncio
async def test_forcenew_mismatch_maps_to_destructive_tier_in_pending_approval():
    discovered = {"id": "vpc-1", "resource_type": "aws_vpc", "cidr_block": "10.0.0.0/16", "tags": []}
    live = {"cidr_block": "172.16.0.0/16", "tags": []}
    state = _base_state(
        resources=[discovered],
        classification_results={"classifications": [_classification("vpc-1", "import")]},
    )
    with patch("agents.drift_reconciliation_agent.fetch_live_resource", return_value=live):
        result = await drift_reconciliation_agent_node(state)

    assert result["pending_approval"]["findings"][0]["tier"] == "destructive"
    assert result["repair_risk_tier"] == "destructive"


@pytest.mark.asyncio
async def test_non_forcenew_mismatch_maps_to_behavior_changing_tier():
    discovered = {"id": "i-1", "resource_type": "aws_instance", "instance_type": "t3.micro", "ami": "ami-123", "tags": []}
    live = {"instance_type": "t3.large", "ami": "ami-123", "tags": []}
    state = _base_state(
        resources=[discovered],
        classification_results={"classifications": [_classification("i-1", "import")]},
    )
    with patch("agents.drift_reconciliation_agent.fetch_live_resource", return_value=live):
        result = await drift_reconciliation_agent_node(state)

    assert result["pending_approval"]["findings"][0]["tier"] == "behavior_changing"
    assert result["repair_risk_tier"] == "behavior_changing"


@pytest.mark.asyncio
async def test_informational_only_drift_never_halts():
    discovered = {"id": "vpc-1", "resource_type": "aws_vpc", "cidr_block": "10.0.0.0/16", "tags": [{"Key": "Owner", "Value": "a"}]}
    live = {"cidr_block": "10.0.0.0/16", "tags": [{"Key": "Owner", "Value": "b"}]}
    state = _base_state(
        resources=[discovered],
        classification_results={"classifications": [_classification("vpc-1", "import")]},
    )
    with patch("agents.drift_reconciliation_agent.fetch_live_resource", return_value=live):
        result = await drift_reconciliation_agent_node(state)

    assert result.get("pending_approval") is None
    assert result["current_agent"] == "plan_equivalence_agent"
    assert result["drift_results"]["informational_count"] == 1


@pytest.mark.asyncio
async def test_merges_into_existing_pending_approval_from_repair_agent():
    existing_finding = {
        "tool": "tfsec", "rule_id": "AVD-AWS-0080", "severity": "HIGH",
        "description": "storage not encrypted", "resource": "aws_db_instance.other", "tier": "destructive",
    }
    discovered = {"id": "vpc-1", "resource_type": "aws_vpc", "cidr_block": "10.0.0.0/16", "tags": []}
    live = {"cidr_block": "172.16.0.0/16", "tags": []}
    state = _base_state(
        resources=[discovered],
        classification_results={"classifications": [_classification("vpc-1", "import")]},
        pending_approval={"reason": "repair_requires_human_approval", "findings": [existing_finding]},
        repair_risk_tier="destructive",
    )
    with patch("agents.drift_reconciliation_agent.fetch_live_resource", return_value=live):
        result = await drift_reconciliation_agent_node(state)

    findings = result["pending_approval"]["findings"]
    assert len(findings) == 2
    assert existing_finding in findings


@pytest.mark.asyncio
async def test_ignores_resources_not_adopted():
    # classified "manual_review" (not "import"/"data_source") - drift never
    # checks it, regardless of any attribute mismatch.
    discovered = {"id": "vpc-1", "resource_type": "aws_vpc", "cidr_block": "10.0.0.0/16", "tags": []}
    state = _base_state(
        resources=[discovered],
        classification_results={"classifications": [_classification("vpc-1", "manual_review")]},
    )
    result = await drift_reconciliation_agent_node(state)
    assert result["drift_results"]["skipped"] is True


@pytest.mark.asyncio
async def test_unsupported_resource_type_is_ignored():
    discovered = {"id": "role-1", "resource_type": "aws_iam_role", "tags": []}
    state = _base_state(
        resources=[discovered],
        classification_results={"classifications": [_classification("role-1", "import")]},
    )
    result = await drift_reconciliation_agent_node(state)
    assert result["drift_results"]["skipped"] is True


# --- Regression guard: real composer + real drift agent, back-to-back ------

@pytest.mark.asyncio
async def test_real_composer_output_produces_zero_drift_for_unchanged_resource():
    """The core safety net for this agent's _FIELD_MAP table: runs the REAL
    terraform_composer_node on a resource, then feeds the SAME resource back
    as the "live" value - since nothing changed, drift must be zero. If
    someone changes a composer template's field or default without updating
    _FIELD_MAP to match, this test starts failing immediately instead of
    silently drifting out of sync."""
    from agents.terraform_composer import terraform_composer_node

    vpc = {
        "id": "vpc-12345678", "resource_type": "aws_vpc", "name": "test-vpc",
        "cidr_block": "10.20.0.0/16", "tags": [{"Key": "Name", "Value": "test-vpc"}],
    }
    instance = {
        "id": "i-12345678", "resource_type": "aws_instance", "name": "test-instance",
        "ami": "ami-0abcdef1234567890", "instance_type": "t3.small", "tags": [],
    }
    composer_state = {
        "job_id": "test", "region": "us-east-1",
        "resources": [vpc, instance],
        "classification_results": {"classifications": []},
        "dependency_graph": {},
        "terraform_binary": "terraform",
    }
    composer_result = await terraform_composer_node(composer_state)

    drift_state = _base_state(
        resources=[vpc, instance],
        classification_results={
            "classifications": [
                _classification("vpc-12345678", "import"),
                _classification("i-12345678", "import"),
            ]
        },
    )

    def _fake_fetch(session, resource_type, resource_id, endpoint_url=None):
        # "Live" AWS returns EXACTLY what was discovered - nothing changed
        # between discovery and this check.
        return vpc if resource_id == "vpc-12345678" else instance

    with patch("agents.drift_reconciliation_agent.fetch_live_resource", side_effect=_fake_fetch):
        drift_result = await drift_reconciliation_agent_node(drift_state)

    assert drift_result["drift_results"]["destructive_equivalent_count"] == 0
    assert drift_result["drift_results"]["behavior_changing_count"] == 0
    assert drift_result.get("pending_approval") is None
    # Sanity: the composer really did generate real HCL for both resources
    # (not an empty/failed run that would make this test meaningless).
    all_hcl = "\n".join(composer_result["terraform_files"].values())
    assert "10.20.0.0/16" in all_hcl
    assert "t3.small" in all_hcl
