"""Unit tests for risk-tiered repair (agents/repair_agent.py).

Guards the core safety property this feature exists for: a destructive- or
behavior_changing-tagged finding must never reach the LLM auto-fix path or
appear changed in the diffed HCL - it must land in pending_approval instead.
"""

import pytest

from agents.repair_agent import (
    _actionable_findings,
    _classify_repair_tier,
    repair_agent_node,
)


def _finding(rule_id="", description="", severity="HIGH", tool="tfsec", resource="aws_db_instance.mydb"):
    return {
        "tool": tool,
        "rule_id": rule_id,
        "severity": severity,
        "description": description,
        "resource": resource,
    }


class TestClassifyRepairTier:
    def test_destructive_keyword_in_description(self):
        f = _finding(description="RDS instance storage_encrypted should be enabled")
        assert _classify_repair_tier(f) == "destructive"

    def test_destructive_keyword_in_rule_id(self):
        # rule_id and description are concatenated before matching - a
        # keyword landing in either field must be caught.
        f = _finding(rule_id="CKV_AWS_engine_version_check", description="")
        assert _classify_repair_tier(f) == "destructive"

    def test_behavior_changing_keyword(self):
        f = _finding(description="Security group allows unrestricted ingress from 0.0.0.0/0")
        assert _classify_repair_tier(f) == "behavior_changing"

    def test_unknown_finding_escalates_to_behavior_changing_not_safe(self):
        f = _finding(rule_id="XYZ123", description="Some entirely novel check with no known keywords")
        assert _classify_repair_tier(f) == "behavior_changing"

    def test_rds_storage_encryption_is_destructive_not_behavior_changing(self):
        # Regression guard for a real bug found by running actual tfsec/
        # checkov against genuine generated HCL (not a hand-written test
        # fixture): CKV_AWS_16 and AVD-AWS-0080 - the two real-world tools'
        # actual wording for "enable RDS storage encryption" - neither
        # contains the literal string "storage_encrypted", so the generic
        # "encrypt" behavior_changing keyword caught them first and
        # misclassified the single most common real destructive RDS finding.
        checkov_wording = _finding(
            rule_id="CKV_AWS_16",
            description="Ensure all data stored in the RDS is securely encrypted at rest",
            resource="aws_db_instance.my_db_490d479c",
        )
        tfsec_wording = _finding(
            rule_id="AVD-AWS-0080",
            description="Instance does not have storage encryption enabled.",
            resource="aws_db_instance.my_db_490d479c",
        )
        assert _classify_repair_tier(checkov_wording) == "destructive"
        assert _classify_repair_tier(tfsec_wording) == "destructive"

    def test_encryption_on_non_database_resource_stays_behavior_changing(self):
        # The DB-specific destructive override must not over-fire on
        # resource types where encryption genuinely can be added in place
        # (e.g. an S3 bucket, via a separate resource - see
        # terraform_composer.py's deterministic template).
        f = _finding(description="Bucket does not have encryption enabled", resource="aws_s3_bucket.data")
        assert _classify_repair_tier(f) == "behavior_changing"

    def test_safe_auto_keyword_is_reachable(self):
        # Regression guard: safe_auto must be a real, reachable outcome, not
        # just a label that's never actually returned - a finding purely
        # about tagging doesn't change resource behavior.
        f = _finding(description="Resource is missing required tag 'Owner'")
        assert _classify_repair_tier(f) == "safe_auto"

    def test_destructive_takes_priority_over_behavior_changing_keywords(self):
        # Contains both a behavior_changing keyword ("public") and a
        # destructive one ("identifier") - destructive must win.
        f = _finding(description="Publicly accessible identifier should be rotated")
        assert _classify_repair_tier(f) == "destructive"


@pytest.mark.asyncio
async def test_destructive_finding_never_reaches_llm_and_lands_in_pending_approval(monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("LLM must never be called for a destructive-tier finding")

    import agents.repair_agent as repair_module
    monkeypatch.setattr(repair_module, "_repair_block_via_llm", fail_if_called)

    tf_files = {
        "data.tf": 'resource "aws_db_instance" "mydb" {\n  identifier = "mydb"\n}',
    }
    state = {
        "job_id": "test",
        "terraform_files": tf_files,
        "security_results": {
            "findings": [
                _finding(
                    description="storage_encrypted should be true for aws_db_instance",
                    resource="aws_db_instance.mydb",
                )
            ]
        },
        "repair_attempts": 0,
        "completed_agents": [],
    }

    result = await repair_agent_node(state)

    assert result["terraform_files"]["data.tf"] == tf_files["data.tf"]  # untouched
    assert result["pending_approval"] is not None
    assert result["pending_approval"]["findings"][0]["tier"] == "destructive"
    assert result["repair_risk_tier"] == "destructive"
    # current_agent must point at the "awaiting_approval" halt hint (matching
    # graph.py::route_after_repair's routing to END), not validation_agent -
    # re-validating would just rediscover the same untouched finding - and
    # not cost_agent either, since the pipeline no longer silently continues
    # past an unresolved destructive finding.
    assert result["current_agent"] == "awaiting_approval"
    assert result["status"] == "AWAITING_APPROVAL"


@pytest.mark.asyncio
async def test_behavior_changing_finding_also_escalates_not_auto_applied(monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("LLM must never be called for a behavior_changing-tier finding")

    import agents.repair_agent as repair_module
    monkeypatch.setattr(repair_module, "_repair_block_via_llm", fail_if_called)

    tf_files = {
        "data.tf": 'resource "aws_db_instance" "mydb" {\n  publicly_accessible = true\n}',
    }
    state = {
        "job_id": "test",
        "terraform_files": tf_files,
        "security_results": {
            "findings": [
                _finding(description="RDS instance should not be publicly accessible", resource="aws_db_instance.mydb")
            ]
        },
        "repair_attempts": 0,
        "completed_agents": [],
    }

    result = await repair_agent_node(state)

    assert result["terraform_files"]["data.tf"] == tf_files["data.tf"]
    assert result["pending_approval"]["findings"][0]["tier"] == "behavior_changing"
    assert result["repair_risk_tier"] == "behavior_changing"
    assert result["current_agent"] == "awaiting_approval"
    assert result["status"] == "AWAITING_APPROVAL"


@pytest.mark.asyncio
async def test_no_findings_means_no_pending_approval():
    state = {
        "job_id": "test",
        "terraform_files": {"data.tf": "resource \"aws_s3_bucket\" \"b\" {\n  bucket = \"b\"\n}"},
        "security_results": {"findings": []},
        "repair_attempts": 0,
        "completed_agents": [],
    }

    result = await repair_agent_node(state)
    # Nothing escalated - current_agent must correctly point back at
    # validation_agent (matching route_after_repair's routing), not stay
    # stuck on whatever policy_agent had already set.
    assert result["current_agent"] == "validation_agent"

    assert result["pending_approval"] is None
    assert result["repair_risk_tier"] is None


@pytest.mark.asyncio
async def test_safe_auto_finding_is_the_only_tier_that_reaches_the_llm(monkeypatch):
    import agents.repair_agent as repair_module

    fixed_block = 'resource "aws_dynamodb_table" "t" {\n  name = "t"\n  tags = { Owner = "team-x" }\n}'
    called = {}

    async def fake_llm(block, error_message):
        called["block"] = block
        return fixed_block

    monkeypatch.setattr(repair_module, "_repair_block_via_llm", fake_llm)

    tf_files = {"data.tf": 'resource "aws_dynamodb_table" "t" {\n  name = "t"\n}'}
    state = {
        "job_id": "test",
        "terraform_files": tf_files,
        "security_results": {
            "findings": [
                _finding(description="Resource is missing required tag 'Owner'", resource="aws_dynamodb_table.t")
            ]
        },
        "repair_attempts": 0,
        "completed_agents": [],
    }

    result = await repair_agent_node(state)

    assert called.get("block"), "LLM should have been called for a safe_auto finding"
    assert result["terraform_files"]["data.tf"] == fixed_block
    assert result["pending_approval"] is None
    assert result["repair_risk_tier"] is None


def test_route_after_repair_halts_when_pending_approval_set():
    from agents.graph import route_after_repair

    # Regardless of how many attempts remain, escalated findings were never
    # touched - looping back to the verifier would just rediscover the exact
    # same findings, and packaging would ship a "COMPLETE" bundle nobody has
    # actually approved. "halt" is mapped to END in build_graph().
    state = {"pending_approval": {"reason": "repair_requires_human_approval", "findings": []}, "repair_attempts": 1}
    assert route_after_repair(state) == "halt"


def test_route_after_repair_reverifies_when_nothing_escalated():
    from agents.graph import route_after_repair

    # Every repair is re-verified; the iteration bound lives in
    # route_after_verify (see the next test).
    assert route_after_repair({"pending_approval": None, "repair_attempts": 1}) == "verifier"


def test_route_after_verify_bounds_the_repair_loop():
    from agents.graph import route_after_verify

    failing = {"pending_approval": None, "validation_results": {"passed": False, "checks": []}, "max_repair_iterations": 2}
    assert route_after_verify({**failing, "repair_attempts": 1}) == "repair"
    assert route_after_verify({**failing, "repair_attempts": 2}) == "package"


def test_actionable_findings_still_filters_severity_and_tool():
    # Unrelated to tiering - confirms the pre-existing filter this feature
    # builds on top of is untouched.
    findings = {
        "findings": [
            _finding(severity="LOW"),
            _finding(tool="trivy"),
            _finding(severity="HIGH", tool="checkov", resource="aws_iam_role.x"),
        ]
    }
    actionable = _actionable_findings(findings)
    assert len(actionable) == 1
    assert actionable[0]["resource"] == "aws_iam_role.x"


def test_route_after_verify_halts_on_plan_equivalence_or_drift_approval():
    # Replaces the old plan_gate/drift_gate edges: the verifier stops its pass
    # the moment either step sets pending_approval, and routing sends it
    # straight to END - never to repair, never to package.
    from agents.graph import route_after_verify

    for reason in ("plan_equivalence_requires_human_approval", "drift_reconciliation_requires_human_approval"):
        state = {
            "pending_approval": {"reason": reason, "findings": []},
            "validation_results": {"passed": False, "checks": []},
            "repair_attempts": 0,
        }
        assert route_after_verify(state) == "halt"

    assert route_after_verify({"pending_approval": None, "validation_results": {"passed": True, "checks": []}}) == "package"
