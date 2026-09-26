"""Unit tests for Intent Analyzer tool and router integration."""

import pytest
from tools.intent_analyzer import parse_deterministic_intent, analyze_user_intent
from agents.intent_router import intent_router_node


@pytest.mark.asyncio
async def test_parse_deterministic_intent_ec2_and_fargate():
    request_text = "Increase EC2 web server from t2.micro to t2.medium and scale Fargate from 2 to 4 tasks."
    result = parse_deterministic_intent(
        user_request=request_text,
        region="us-east-1",
        environment="production",
        resource_filters=["EC2", "ECS", "VPC", "SG"],
    )

    assert result["operation"] == "modify"
    assert result["operation_label"] == "Modify Infrastructure"
    assert result["region"] == "us-east-1"
    assert result["environment"] == "production"

    # Check target resources
    res_names = [r["resource_name"].lower() for r in result["target_resources"]]
    assert any("web server" in name for name in res_names)
    assert any("fargate" in name for name in res_names)

    # Check requested changes
    attributes = [c["attribute"] for c in result["requested_changes"]]
    assert "instance_type" in attributes
    assert "desired_count" in attributes

    change_ec2 = next(c for c in result["requested_changes"] if c["attribute"] == "instance_type")
    assert change_ec2["current_value"] == "t2.micro"
    assert change_ec2["target_value"] == "t2.medium"

    change_ecs = next(c for c in result["requested_changes"] if c["attribute"] == "desired_count")
    assert change_ecs["current_value"] == "2"
    assert change_ecs["target_value"] == "4"


@pytest.mark.asyncio
async def test_intent_router_node_with_user_request():
    state = {
        "job_id": "test-job-123",
        "operation": "modify",
        "region": "us-west-2",
        "environment": "staging",
        "user_request": "Increase EC2 web server from t2.micro to t2.medium and scale Fargate from 2 to 4 tasks.",
        "resource_filters": ["EC2", "ECS", "VPC"],
        "completed_agents": [],
    }

    result = await intent_router_node(state)

    assert "intent" in result
    intent = result["intent"]
    assert intent["operation"] == "modify"
    assert intent["region"] == "us-west-2"
    assert intent["environment"] == "staging"
    assert len(intent["target_resources"]) >= 2
    assert len(intent["requested_changes"]) >= 2
    assert "intent_router" in result["completed_agents"]
    assert result["progress_percentage"] == 10
