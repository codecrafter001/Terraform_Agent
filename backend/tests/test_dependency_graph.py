"""Unit tests for DependencyGraphBuilder: relationships, edge confidence, evidence,
configurable thresholds, and cycle handling.
"""

import pytest

from tools.graph_builder import DependencyGraphBuilder


def test_empty_resources():
    builder = DependencyGraphBuilder()
    graph_data = builder.build_graph([])

    assert graph_data["node_count"] == 0
    assert graph_data["edge_count"] == 0
    assert graph_data["nodes"] == []
    assert graph_data["links"] == []
    assert graph_data["is_dag"] is True
    assert graph_data["cycles"] == []


def test_vpc_subnet_ec2_chain_relationships():
    resources = [
        {"id": "vpc-1", "resource_type": "aws_vpc", "name": "main-vpc"},
        {"id": "subnet-1", "resource_type": "aws_subnet", "name": "public-1", "vpc_id": "vpc-1"},
        {"id": "sg-1", "resource_type": "aws_security_group", "name": "web-sg", "vpc_id": "vpc-1"},
        {
            "id": "i-1",
            "resource_type": "aws_instance",
            "name": "web-server",
            "vpc_id": "vpc-1",
            "subnet_id": "subnet-1",
            "security_groups": ["sg-1"],
            "iam_instance_profile_arn": "arn:aws:iam::123456789012:instance-profile/web-role",
        },
        {
            "id": "web-role",
            "resource_type": "aws_iam_role",
            "name": "web-role",
            "assume_role_policy": {
                "Statement": [{"Principal": {"Service": "ec2.amazonaws.com"}}]
            }
        }
    ]

    builder = DependencyGraphBuilder(confidence_threshold=0.80)
    graph_data = builder.build_graph(resources)

    assert graph_data["node_count"] == 5
    assert graph_data["is_dag"] is True

    # Verify all edges & confidence
    link_map = {(l["source"], l["target"]): l for l in graph_data["links"]}

    # VPC -> Subnet (contains, 1.0)
    assert ("vpc-1", "subnet-1") in link_map
    assert link_map[("vpc-1", "subnet-1")]["confidence"] == 1.0
    assert link_map[("vpc-1", "subnet-1")]["is_trusted"] is True
    assert "explicit AWS API relationship" in link_map[("vpc-1", "subnet-1")]["evidence"]

    # Subnet -> EC2 (hosted_in, 1.0)
    assert ("subnet-1", "i-1") in link_map
    assert link_map[("subnet-1", "i-1")]["confidence"] == 1.0
    assert link_map[("subnet-1", "i-1")]["is_trusted"] is True

    # Security Group -> EC2 (secured_by, 1.0)
    assert ("sg-1", "i-1") in link_map
    assert link_map[("sg-1", "i-1")]["confidence"] == 1.0
    assert link_map[("sg-1", "i-1")]["is_trusted"] is True

    # IAM Role -> EC2 (assumes_role, 0.80)
    assert ("web-role", "i-1") in link_map
    assert link_map[("web-role", "i-1")]["confidence"] == 0.80
    assert link_map[("web-role", "i-1")]["is_trusted"] is True
    assert "heuristic" in link_map[("web-role", "i-1")]["evidence"]


def test_route_table_relationships():
    resources = [
        {"id": "vpc-1", "resource_type": "aws_vpc"},
        {"id": "subnet-1", "resource_type": "aws_subnet", "vpc_id": "vpc-1"},
        {
            "id": "rt-1",
            "resource_type": "aws_route_table",
            "vpc_id": "vpc-1",
            "associated_subnets": ["subnet-1"]
        }
    ]

    builder = DependencyGraphBuilder()
    graph_data = builder.build_graph(resources)

    link_map = {(l["source"], l["target"]): l for l in graph_data["links"]}
    assert ("vpc-1", "rt-1") in link_map
    assert ("rt-1", "subnet-1") in link_map
    assert link_map[("rt-1", "subnet-1")]["relation"] == "routes_subnet"
    assert link_map[("rt-1", "subnet-1")]["confidence"] == 1.0


def test_tag_based_relationship_inference():
    resources = [
        {"id": "bucket-prod", "resource_type": "aws_s3_bucket"},
        {
            "id": "i-worker",
            "resource_type": "aws_instance",
            "tags": [{"Key": "StorageBucket", "Value": "bucket-prod"}]
        }
    ]

    builder = DependencyGraphBuilder(confidence_threshold=0.80)
    graph_data = builder.build_graph(resources)

    link_map = {(l["source"], l["target"]): l for l in graph_data["links"]}
    assert ("bucket-prod", "i-worker") in link_map
    link = link_map[("bucket-prod", "i-worker")]
    assert link["confidence"] == 0.70
    assert link["is_trusted"] is False  # 0.70 < 0.80 threshold
    assert "Tag relationship" in link["evidence"]


def test_confidence_threshold_trusted_vs_advisory():
    resources = [
        {"id": "role-1", "resource_type": "aws_iam_role"},
        {
            "id": "i-1",
            "resource_type": "aws_instance",
            "iam_instance_profile_arn": "arn:aws:iam::123:instance-profile/role-1"
        }
    ]

    # Threshold 0.80: confidence 0.80 is trusted
    b_low = DependencyGraphBuilder(confidence_threshold=0.80)
    g_low = b_low.build_graph(resources)
    assert g_low["links"][0]["is_trusted"] is True
    assert g_low["high_confidence_edge_count"] == 1
    assert g_low["advisory_edge_count"] == 0

    # Strict threshold 0.90: confidence 0.80 is advisory
    b_high = DependencyGraphBuilder(confidence_threshold=0.90)
    g_high = b_high.build_graph(resources)
    assert g_high["links"][0]["is_trusted"] is False
    assert g_high["high_confidence_edge_count"] == 0
    assert g_high["advisory_edge_count"] == 1


def test_cyclic_relationship_handling():
    # Observed circular reference in tags
    resources = [
        {
            "id": "res-a",
            "resource_type": "aws_instance",
            "tags": [{"Key": "DependsOn", "Value": "res-b"}]
        },
        {
            "id": "res-b",
            "resource_type": "aws_instance",
            "tags": [{"Key": "DependsOn", "Value": "res-a"}]
        }
    ]

    builder = DependencyGraphBuilder()
    graph_data = builder.build_graph(resources)

    assert graph_data["is_dag"] is False
    assert len(graph_data["cycles"]) >= 1
    # Nodes and edges are preserved without loss
    assert graph_data["node_count"] == 2
    assert graph_data["edge_count"] == 2
