"""Regression coverage for documentation_agent.py's import_plan.md generation.

Guards against a real, verified bug: import_cmds used to be built from raw
state["resources"] with no classification awareness, so a resource
classified "skip" or "data_source" (which terraform_composer.py correctly
gives no `resource` block, or a `data` block instead) still got a
`terraform import` command - one that always fails, since there's no
resource address in the generated configuration to bind to.
"""

import pytest

import agents.documentation_agent as doc_module
from agents.documentation_agent import documentation_agent_node


def _resource(resource_type, resource_id, **extra):
    return {"resource_type": resource_type, "id": resource_id, "name": resource_id, **extra}


@pytest.mark.asyncio
async def test_import_plan_excludes_skip_and_data_source_resources():
    resources = [
        _resource("aws_vpc", "vpc-1", cidr_block="10.0.0.0/16"),
        _resource("aws_security_group", "sg-default1", vpc_id="vpc-1", ip_permissions=[], ip_permissions_egress=[]),
        _resource("aws_iam_role", "role-svc", arn="arn:aws:iam::123:role/aws-service-role/x/role-svc"),
    ]
    classification_results = {
        "classifications": [
            {"resource_id": "vpc-1", "resource_type": "aws_vpc", "category": "unmanaged", "reason": [], "recommended_action": "import"},
            {"resource_id": "sg-default1", "resource_type": "aws_security_group", "category": "shared", "reason": [], "recommended_action": "data_source"},
            {"resource_id": "role-svc", "resource_type": "aws_iam_role", "category": "shared", "reason": [], "recommended_action": "skip"},
        ]
    }
    state = {
        "job_id": "test",
        "region": "us-east-1",
        "resources": resources,
        "terraform_files": {"providers.tf": "", "variables.tf": ""},
        "dependency_graph": {},
        "classification_results": classification_results,
        "adoption_plan": {},
        "validation_results": {"passed": True},
        "security_results": {"risk_score": 0},
        "completed_agents": [],
    }

    result = await documentation_agent_node(state)
    import_plan = result["documentation"]["migration/import_plan.md"]

    assert "vpc-1" in import_plan
    assert "sg-default1" not in import_plan
    assert "role-svc" not in import_plan


@pytest.mark.asyncio
async def test_import_plan_groups_by_wave_and_separates_manual_review():
    resources = [
        _resource("aws_vpc", "vpc-1"),
        _resource("aws_subnet", "subnet-1", vpc_id="vpc-1"),
        _resource("aws_iam_role", "role-orphan"),
    ]
    classification_results = {
        "classifications": [
            {"resource_id": "vpc-1", "resource_type": "aws_vpc", "category": "unmanaged", "reason": [], "recommended_action": "import"},
            {"resource_id": "subnet-1", "resource_type": "aws_subnet", "category": "unmanaged", "reason": [], "recommended_action": "import"},
            {"resource_id": "role-orphan", "resource_type": "aws_iam_role", "category": "orphaned", "reason": [], "recommended_action": "manual_review"},
        ]
    }
    adoption_plan = {
        "categories": [],
        "total_resource_count": 3,
        "risk_score": 33,
        "import_order": ["vpc-1", "subnet-1"],
        "waves": [
            {"wave": 1, "resource_ids": ["vpc-1"], "risk_level": "low", "risk_signals": []},
            {"wave": 2, "resource_ids": ["subnet-1"], "risk_level": "low", "risk_signals": []},
        ],
        "summary": None,
    }
    state = {
        "job_id": "test",
        "region": "us-east-1",
        "resources": resources,
        "terraform_files": {"providers.tf": "", "variables.tf": ""},
        "dependency_graph": {},
        "classification_results": classification_results,
        "adoption_plan": adoption_plan,
        "validation_results": {"passed": True},
        "security_results": {"risk_score": 0},
        "completed_agents": [],
    }

    result = await documentation_agent_node(state)
    import_plan = result["documentation"]["migration/import_plan.md"]

    vpc_idx = import_plan.index("vpc-1")
    subnet_idx = import_plan.index("subnet-1")
    manual_review_idx = import_plan.index("Manual Review Required")
    role_idx = import_plan.index("role-orphan")

    assert vpc_idx < subnet_idx < manual_review_idx < role_idx
    assert "Wave 1" in import_plan
    assert "Wave 2" in import_plan


@pytest.mark.asyncio
async def test_readme_always_shows_pending_approval_even_if_llm_truncates(monkeypatch):
    """Regression guard for a real bug found by running the full pipeline
    end-to-end with a genuine destructive finding: a real Gemini response
    truncated after the very first README section (num_predict was too
    small for 6+ required sections), so "Pending Human Approval" - the one
    thing this whole feature exists to surface - silently never appeared,
    even though state["pending_approval"] correctly held the finding.
    Simulates that exact truncation (an LLM response with only the first
    section, no mention of approval at all) and asserts the final README
    still contains the real disclosure regardless."""

    async def truncated_llm_response(prompt, system=None, num_predict=None, **kwargs):
        return "# Infrastructure Overview\n\n- **Total Discovered Resources:** 1\n- **Main Services:** Amazon Rel"

    monkeypatch.setattr(doc_module.ollama_client, "generate", truncated_llm_response)

    resources = [_resource("aws_db_instance", "db-1")]
    state = {
        "job_id": "test",
        "region": "us-east-1",
        "resources": resources,
        "terraform_files": {"providers.tf": "", "application.tf": ""},
        "dependency_graph": {},
        "classification_results": {"classifications": []},
        "adoption_plan": {},
        "validation_results": {"passed": True},
        "security_results": {"risk_score": 30},
        "cost_results": {},
        "pending_approval": {
            "reason": "repair_requires_human_approval",
            "findings": [
                {
                    "tool": "tfsec", "rule_id": "AVD-AWS-0080", "severity": "HIGH",
                    "description": "Instance does not have storage encryption enabled.",
                    "resource": "aws_db_instance.db_1", "tier": "destructive",
                }
            ],
        },
        "repair_risk_tier": "destructive",
        "completed_agents": [],
    }

    result = await documentation_agent_node(state)
    readme = result["documentation"]["README.md"]

    assert "## Pending Human Approval" in readme
    assert "AVD-AWS-0080" in readme
    assert "destructive" in readme
    # The LLM's own truncated content should still be present (not discarded
    # entirely) - only the missing section was deterministically backfilled.
    assert "Amazon Rel" in readme


@pytest.mark.asyncio
async def test_rejected_job_readme_never_carries_adoption_instructions(monkeypatch):
    """Regression guard for a real bug found by inspecting a live rejected
    job's actual results: the README (and therefore the ZIP built from it)
    still carried the full "Safe Import & Adoption Instructions" apply guide
    even though a human explicitly rejected the configuration - the bundle
    looked exactly as adoption-ready as an approved one. A rejected job's
    README must swap that section for a clear rejection notice instead."""

    async def fake_llm(prompt, system=None, num_predict=None, **kwargs):
        return "# Infrastructure Overview\n\n- **Total Discovered Resources:** 1"

    monkeypatch.setattr(doc_module.ollama_client, "generate", fake_llm)

    resources = [_resource("aws_iam_role", "orphan-role")]
    state = {
        "job_id": "test",
        "region": "us-east-1",
        "resources": resources,
        "terraform_files": {"application.tf": ""},
        "dependency_graph": {},
        "classification_results": {"classifications": []},
        "adoption_plan": {},
        "validation_results": {"passed": True},
        "security_results": {"risk_score": 40},
        "cost_results": {},
        "pending_approval": {
            "reason": "plan_equivalence_requires_human_approval",
            "findings": [
                {
                    "tool": "terraform_plan", "rule_id": "plan-equivalence", "severity": "HIGH",
                    "description": "destroy action against aws_iam_role.orphan_role",
                    "resource": "aws_iam_role.orphan_role", "tier": "destructive",
                }
            ],
        },
        "repair_risk_tier": "destructive",
        "approval_decision": {
            "decision": "rejected", "reason": "Too risky, not adopting this",
            "decided_at": "2026-01-01T00:00:00",
        },
        "completed_agents": [],
    }

    result = await documentation_agent_node(state)
    readme = result["documentation"]["README.md"]

    assert "Safe Import & Adoption Instructions" not in readme
    assert "## Not Approved For Adoption" in readme
    assert "REJECTED" in readme
    assert "Too risky, not adopting this" in readme
    # The finding itself must still be visible for the audit trail.
    assert "aws_iam_role.orphan_role" in readme


@pytest.mark.asyncio
async def test_approved_job_readme_still_carries_adoption_instructions(monkeypatch):
    """An approved job is the normal case - it must keep the real adoption
    guide, not the rejection notice, and should show the approval decision
    alongside the (now-approved) finding."""

    async def fake_llm(prompt, system=None, num_predict=None, **kwargs):
        return "# Infrastructure Overview\n\n- **Total Discovered Resources:** 1"

    monkeypatch.setattr(doc_module.ollama_client, "generate", fake_llm)

    state = {
        "job_id": "test",
        "region": "us-east-1",
        "resources": [_resource("aws_db_instance", "db-1")],
        "terraform_files": {"application.tf": ""},
        "dependency_graph": {},
        "classification_results": {"classifications": []},
        "adoption_plan": {},
        "validation_results": {"passed": True},
        "security_results": {"risk_score": 20},
        "cost_results": {},
        "pending_approval": {
            "reason": "plan_equivalence_requires_human_approval",
            "findings": [
                {
                    "tool": "terraform_plan", "rule_id": "plan-equivalence", "severity": "HIGH",
                    "description": "replace action against aws_db_instance.db_1",
                    "resource": "aws_db_instance.db_1", "tier": "behavior_changing",
                }
            ],
        },
        "repair_risk_tier": "behavior_changing",
        "approval_decision": {
            "decision": "approved", "reason": "Reviewed, safe to proceed",
            "decided_at": "2026-01-01T00:00:00",
        },
        "completed_agents": [],
    }

    result = await documentation_agent_node(state)
    readme = result["documentation"]["README.md"]

    assert "## Safe Import & Adoption Instructions" in readme
    assert "## Not Approved For Adoption" not in readme
    assert "APPROVED" in readme
    assert "Reviewed, safe to proceed" in readme
