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
    # graph.py::repair_or_done's own routing to END), not validation_agent -
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
    # validation_agent (matching repair_or_done's own routing), not stay
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


def test_repair_or_done_routes_to_halt_when_pending_approval_set():
    from agents.graph import repair_or_done

    # Regardless of how many attempts remain, escalated findings were never
    # touched - looping back to validation_agent would just rediscover the
    # exact same findings, and continuing on to cost_agent would ship a
    # "COMPLETE" bundle nobody has actually approved. "halt" is mapped to
    # END in build_graph()'s conditional edges.
    state = {"pending_approval": {"reason": "repair_requires_human_approval", "findings": []}, "repair_attempts": 1}
    assert repair_or_done(state) == "halt"


def test_repair_or_done_still_loops_when_nothing_escalated():
    from agents.graph import repair_or_done

    state = {"pending_approval": None, "repair_attempts": 1}
    assert repair_or_done(state) == "validation_agent"

    state = {"pending_approval": None, "repair_attempts": 3}
    assert repair_or_done(state) == "cost_agent"


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


def test_plan_gate_halts_when_plan_equivalence_set_pending_approval():
    from agents.graph import plan_gate

    state = {"pending_approval": {"reason": "plan_equivalence_requires_human_approval", "findings": []}}
    assert plan_gate(state) == "halt"

    assert plan_gate({"pending_approval": None}) == "policy_agent"


def test_drift_gate_halts_when_drift_reconciliation_set_pending_approval():
    from agents.graph import drift_gate

    state = {"pending_approval": {"reason": "drift_reconciliation_requires_human_approval", "findings": []}}
    assert drift_gate(state) == "halt"

    assert drift_gate({"pending_approval": None}) == "plan_equivalence_agent"


@pytest.mark.asyncio
async def test_repair_agent_merges_into_existing_pending_approval_from_plan_equivalence(monkeypatch):
    # plan_equivalence_agent now runs BEFORE policy_agent/repair_agent in the
    # pipeline, so by the time repair_agent runs, state["pending_approval"]
    # may already carry a plan-diff finding nobody has reviewed yet. A
    # repair-cycle escalation must extend that list, never silently replace
    # it - losing an earlier, unrelated finding would be a real regression.
    import agents.repair_agent as repair_module
    monkeypatch.setattr(repair_module, "_repair_block_via_llm", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

    tf_files = {"data.tf": 'resource "aws_db_instance" "mydb" {\n  publicly_accessible = true\n}'}
    plan_finding = {
        "tool": "terraform_plan", "rule_id": "plan-equivalence", "severity": "HIGH",
        "description": "terraform plan reports a `destroy` action against `aws_vpc.old`.",
        "resource": "aws_vpc.old", "tier": "destructive",
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
        "pending_approval": {"reason": "plan_equivalence_requires_human_approval", "findings": [plan_finding]},
        "repair_risk_tier": "destructive",
    }

    result = await repair_agent_node(state)

    findings = result["pending_approval"]["findings"]
    assert len(findings) == 2
    assert plan_finding in findings
    assert any(f["resource"] == "aws_db_instance.mydb" for f in findings)
    assert result["repair_risk_tier"] == "destructive"
    assert result["current_agent"] == "awaiting_approval"
