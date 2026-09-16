"""Unit tests for the deterministic adoption planner (tools/adoption_planner.py)."""

import pytest

from tools.adoption_planner import (
    MAX_ADOPTION_WAVE_SIZE,
    _build_waves,
    build_adoption_plan,
    deterministic_summary,
)


def _classification(resource_id, category, action, reason=None):
    return {
        "resource_id": resource_id,
        "resource_type": "aws_vpc",
        "category": category,
        "reason": reason or [],
        "recommended_action": action,
    }


def test_build_waves_orders_by_dependency_depth():
    dependency_graph = {
        "nodes": [
            {"id": "vpc-1", "depth": 0},
            {"id": "subnet-1", "depth": 1},
            {"id": "instance-1", "depth": 2},
        ],
        "links": [
            {"source": "vpc-1", "target": "subnet-1", "relation": "contains", "confidence": 1.0, "is_trusted": True},
            {"source": "subnet-1", "target": "instance-1", "relation": "hosted_in", "confidence": 1.0, "is_trusted": True},
        ],
    }
    resources_by_id = {
        "vpc-1": {"id": "vpc-1", "resource_type": "aws_vpc", "tags": [{"Key": "Name", "Value": "x"}]},
        "subnet-1": {"id": "subnet-1", "resource_type": "aws_subnet", "tags": [{"Key": "Name", "Value": "x"}]},
        "instance-1": {"id": "instance-1", "resource_type": "aws_instance", "tags": [{"Key": "Name", "Value": "x"}]},
    }
    waves, import_order, cycles, _ = _build_waves(["vpc-1", "subnet-1", "instance-1"], dependency_graph, resources_by_id)

    assert [w.wave for w in waves] == [1, 2, 3]
    assert waves[0].resource_ids == ["vpc-1"]
    assert waves[1].resource_ids == ["subnet-1"]
    assert waves[2].resource_ids == ["instance-1"]
    assert import_order == ["vpc-1", "subnet-1", "instance-1"]
    assert len(cycles) == 0


def test_build_waves_advisory_edge_does_not_force_delay():
    # If edge has low confidence (is_trusted=False), it does NOT sequence wave ordering
    dependency_graph = {
        "nodes": [
            {"id": "s3-1", "depth": 0},
            {"id": "instance-1", "depth": 0},
        ],
        "links": [
            {"source": "s3-1", "target": "instance-1", "relation": "tag_reference", "confidence": 0.60, "is_trusted": False},
        ],
    }
    resources_by_id = {
        "s3-1": {"id": "s3-1", "resource_type": "aws_s3_bucket", "tags": [{"Key": "Name", "Value": "s3"}]},
        "instance-1": {"id": "instance-1", "resource_type": "aws_instance", "tags": [{"Key": "Name", "Value": "inst"}]},
    }
    # With confidence threshold 0.80, the 0.60 edge is advisory and should NOT push instance-1 to wave 2
    waves, import_order, _, _ = _build_waves(
        ["s3-1", "instance-1"], dependency_graph, resources_by_id, confidence_threshold=0.80
    )

    assert len(waves) == 1
    assert set(waves[0].resource_ids) == {"s3-1", "instance-1"}
    assert any("advisory/low-confidence" in s for s in waves[0].risk_signals)


def test_build_waves_cycle_detection_moves_to_review():
    # Cycle between two safe-to-import resources
    dependency_graph = {
        "nodes": [{"id": "node-a"}, {"id": "node-b"}, {"id": "node-ok"}],
        "links": [
            {"source": "node-a", "target": "node-b", "confidence": 1.0, "is_trusted": True},
            {"source": "node-b", "target": "node-a", "confidence": 1.0, "is_trusted": True},
        ],
    }
    resources_by_id = {
        "node-a": {"id": "node-a", "resource_type": "aws_instance", "tags": []},
        "node-b": {"id": "node-b", "resource_type": "aws_instance", "tags": []},
        "node-ok": {"id": "node-ok", "resource_type": "aws_instance", "tags": []},
    }

    waves, import_order, cycle_nodes, detected_cycles = _build_waves(
        ["node-a", "node-b", "node-ok"], dependency_graph, resources_by_id
    )

    assert cycle_nodes == {"node-a", "node-b"}
    assert len(detected_cycles) >= 1
    # Only node-ok remains in safe waves
    assert len(waves) == 1
    assert waves[0].resource_ids == ["node-ok"]


def test_build_adoption_plan_full_summary_and_counts():
    classification_report = {
        "classifications": [
            _classification("vpc-1", "unmanaged", "import"),
            _classification("subnet-1", "unmanaged", "import"),
            _classification("sg-default", "shared", "data_source"),
            _classification("role-svc", "shared", "skip"),
            _classification("orphaned-sg", "orphaned", "manual_review"),
            _classification("unsupported-res", "unsupported", "manual_review"),
        ]
    }
    dependency_graph = {
        "nodes": [{"id": "vpc-1"}, {"id": "subnet-1"}],
        "links": [{"source": "vpc-1", "target": "subnet-1", "confidence": 1.0, "is_trusted": True}],
        "edge_count": 1,
        "high_confidence_edge_count": 1,
    }
    resources = [
        {"id": "vpc-1", "resource_type": "aws_vpc", "tags": [{"Key": "Name", "Value": "vpc"}]},
        {"id": "subnet-1", "resource_type": "aws_subnet", "tags": [{"Key": "Name", "Value": "subnet"}]},
        {"id": "sg-default", "resource_type": "aws_security_group", "tags": []},
        {"id": "role-svc", "resource_type": "aws_iam_role", "tags": []},
        {"id": "orphaned-sg", "resource_type": "aws_security_group", "tags": []},
        {"id": "unsupported-res", "resource_type": "aws_kinesis_stream", "tags": []},
    ]

    plan = build_adoption_plan(classification_report, dependency_graph, resources)

    assert plan.total_resource_count == 6
    assert plan.managed_count == 2
    assert plan.data_source_count == 1
    assert plan.do_not_manage_count == 1
    assert plan.review_count == 1
    assert plan.unsupported_count == 1
    assert plan.total_dependencies == 1
    assert plan.high_confidence_dependencies == 1
    assert plan.wave_count == 2  # vpc-1 (depth 0), subnet-1 (depth 1)
    assert plan.risk_score == 33  # (1 review + 1 unsupported) / 6 = 33%

    summary_text = deterministic_summary(plan)
    assert "6 resource(s) discovered" in summary_text
    assert "2 are safe to import directly" in summary_text
    assert "1 should be referenced via data source" in summary_text
    assert "Overall risk score: 33/100" in summary_text
