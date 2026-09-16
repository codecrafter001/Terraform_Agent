"""Negative QA and robustness tests for P1 Infrastructure Intelligence.
Actively stress-tests failure modes, corrupt state, edge cases, malformed data, and cyclic graphs.
"""

import pytest

from agents.adoption_planning_agent import adoption_planning_agent_node
from tools.adoption_planner import build_adoption_plan
from tools.graph_builder import DependencyGraphBuilder
from tools.resource_classifier import classify_resources


def test_empty_environment_safe_handling():
    builder = DependencyGraphBuilder()
    graph_data = builder.build_graph([])
    assert graph_data["node_count"] == 0
    assert graph_data["is_dag"] is True

    plan = build_adoption_plan(
        {"classifications": [], "summary": {}},
        graph_data,
        []
    )
    assert plan.total_resource_count == 0
    assert plan.risk_score == 0
    assert plan.waves == []
    assert plan.categories == []


def test_malformed_and_incomplete_resources():
    malformed_resources = [
        {},  # Completely empty
        {"name": "no-id"},  # Missing id
        {"id": "no-type"},  # Missing resource_type
        {"id": "bad-tags", "resource_type": "aws_vpc", "tags": "not-a-list"},  # Invalid tags
        {
            "id": "bad-iam",
            "resource_type": "aws_iam_role",
            "assume_role_policy": "not-a-dict",  # Corrupted policy
            "tags": None
        },
        {
            "id": "bad-refs",
            "resource_type": "aws_instance",
            "vpc_id": None,
            "subnet_id": None,
            "security_groups": [None, "", 12345],  # Malformed SG references
            "iam_instance_profile_arn": "malformed-arn"
        }
    ]

    builder = DependencyGraphBuilder()
    graph_data = builder.build_graph(malformed_resources)

    # Graph builder must not crash
    assert isinstance(graph_data, dict)
    assert "nodes" in graph_data
    assert "links" in graph_data

    # Classifications for malformed resources must not crash
    report = classify_resources(malformed_resources, graph_data)
    assert isinstance(report.classifications, list)
    for c in report.classifications:
        assert c.category in ["unmanaged", "unsupported", "orphaned", "shared", "managed"]
        assert c.recommended_action in ["import", "data_source", "skip", "manual_review"]


def test_circular_dependency_negative_qa():
    # 3-node cycle in safe_to_import resources
    resources = [
        {"id": "inst-1", "resource_type": "aws_instance", "tags": [{"Key": "DependsOn", "Value": "inst-2"}]},
        {"id": "inst-2", "resource_type": "aws_instance", "tags": [{"Key": "DependsOn", "Value": "inst-3"}]},
        {"id": "inst-3", "resource_type": "aws_instance", "tags": [{"Key": "DependsOn", "Value": "inst-1"}]},
        {"id": "inst-safe", "resource_type": "aws_instance", "tags": []},
    ]

    builder = DependencyGraphBuilder(confidence_threshold=0.50)  # low threshold so tag edges are trusted
    graph_data = builder.build_graph(resources)

    assert graph_data["is_dag"] is False
    assert len(graph_data["cycles"]) >= 1

    classification_report = {
        "classifications": [
            {"resource_id": "inst-1", "resource_type": "aws_instance", "category": "unmanaged", "recommended_action": "import"},
            {"resource_id": "inst-2", "resource_type": "aws_instance", "category": "unmanaged", "recommended_action": "import"},
            {"resource_id": "inst-3", "resource_type": "aws_instance", "category": "unmanaged", "recommended_action": "import"},
            {"resource_id": "inst-safe", "resource_type": "aws_instance", "category": "unmanaged", "recommended_action": "import"},
        ]
    }

    # Adoption planner must safely detect the cycle and move inst-1, 2, 3 to review_required
    plan = build_adoption_plan(classification_report, graph_data, resources, confidence_threshold=0.50)

    # Only inst-safe remains in safe waves
    assert plan.managed_count == 1
    assert plan.review_count == 3
    assert len(plan.waves) == 1
    assert plan.waves[0].resource_ids == ["inst-safe"]
    assert len(plan.cycles_detected) >= 1


@pytest.mark.asyncio
async def test_llm_failure_adoption_planning_fallback(monkeypatch):
    from services.ollama_client import ollama_client

    async def _failing_generate(*args, **kwargs):
        raise TimeoutError("Ollama LLM connection timeout")

    monkeypatch.setattr(ollama_client, "generate", _failing_generate)

    state = {
        "job_id": "negative-test-llm",
        "resources": [{"id": "vpc-1", "resource_type": "aws_vpc"}],
        "classification_results": {
            "classifications": [
                {"resource_id": "vpc-1", "resource_type": "aws_vpc", "category": "unmanaged", "recommended_action": "import"}
            ]
        },
        "dependency_graph": {"nodes": [{"id": "vpc-1"}], "links": []},
        "completed_agents": [],
    }

    # Node must not crash when LLM times out; it must use deterministic summary fallback
    result = await adoption_planning_agent_node(state)
    assert "adoption_plan" in result
    plan = result["adoption_plan"]
    assert plan["total_resource_count"] == 1
    assert plan["managed_count"] == 1
    assert "1 resource(s) discovered" in plan["summary"]
    assert "1 are safe to import directly" in plan["summary"]
