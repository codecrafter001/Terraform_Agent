"""Unit and integration tests for terraform_composer: category gating, data source generation,
and adoption plan integration.
"""

import pytest

from agents.terraform_composer import terraform_composer_node


@pytest.mark.asyncio
async def test_sets_current_agent_to_validation_agent():
    state = {
        "job_id": "test-1",
        "region": "us-east-1",
        "resources": [],
        "classification_results": {},
        "dependency_graph": {},
        "completed_agents": [],
    }

    result = await terraform_composer_node(state)

    assert result["current_agent"] == "validation_agent"


@pytest.mark.asyncio
async def test_composer_respects_adoption_plan_and_classification_gating():
    resources = [
        {"id": "vpc-managed", "resource_type": "aws_vpc", "name": "prod-vpc", "cidr_block": "10.0.0.0/16"},
        {"id": "sg-shared", "resource_type": "aws_security_group", "name": "default-sg"},
        {"id": "role-skipped", "resource_type": "aws_iam_role", "name": "AWSServiceRole"},
        {"id": "sg-orphaned", "resource_type": "aws_security_group", "name": "abandoned-sg"},
        {"id": "custom-unsupported", "resource_type": "aws_unknown_custom", "name": "custom-stream"},
    ]

    state = {
        "job_id": "test-gating",
        "region": "us-east-1",
        "resources": resources,
        "classification_results": {
            "classifications": [
                {"resource_id": "vpc-managed", "resource_type": "aws_vpc", "category": "unmanaged", "recommended_action": "import"},
                {"resource_id": "sg-shared", "resource_type": "aws_security_group", "category": "shared", "recommended_action": "data_source"},
                {"resource_id": "role-skipped", "resource_type": "aws_iam_role", "category": "managed", "recommended_action": "skip"},
                {"resource_id": "sg-orphaned", "resource_type": "aws_security_group", "category": "orphaned", "recommended_action": "manual_review"},
                {"resource_id": "custom-unsupported", "resource_type": "aws_unknown_custom", "category": "unsupported", "recommended_action": "manual_review"},
            ]
        },
        "adoption_plan": {
            "categories": [
                {"category": "safe_to_import", "resource_ids": ["vpc-managed"], "resource_count": 1},
                {"category": "use_data_source", "resource_ids": ["sg-shared"], "resource_count": 1},
                {"category": "do_not_manage", "resource_ids": ["role-skipped"], "resource_count": 1},
                {"category": "review_required", "resource_ids": ["sg-orphaned"], "resource_count": 1},
                {"category": "unsupported", "resource_ids": ["custom-unsupported"], "resource_count": 1},
            ]
        },
        "dependency_graph": {
            "stacks": [{"name": "foundation", "resource_ids": ["vpc-managed", "sg-shared"]}]
        },
        "completed_agents": [],
    }

    result = await terraform_composer_node(state)
    files = result["terraform_files"]

    all_tf_content = "\n\n".join(files.values())

    # 1. safe_to_import (vpc-managed) MUST generate managed resource
    assert 'resource "aws_vpc"' in all_tf_content

    # 2. use_data_source (sg-shared) MUST generate data source block
    assert 'data "aws_security_group"' in all_tf_content
    assert 'resource "aws_security_group" "default_sg' not in all_tf_content

    # 3. do_not_manage (role-skipped) MUST NOT generate anything
    assert "AWSServiceRole" not in all_tf_content
    assert 'resource "aws_iam_role"' not in all_tf_content

    # 4. review_required (sg-orphaned) MUST NOT generate managed resource
    assert 'resource "aws_security_group" "abandoned_sg' not in all_tf_content

    # 5. unsupported (custom-unsupported) MUST NOT generate managed resource
    assert "custom-stream" not in all_tf_content
