"""Unit and integration tests for Resource Classification and downstream Composer gating.

Covers:
- Normal managed resource
- Unmanaged resource
- Shared resource (default SG, default VPC, service-linked role, shared tag)
- Orphaned resource (zero degree, unassumed trust policy)
- Unsupported resource (unsupported type, unknown type)
- Missing metadata (missing id, missing resource_type)
- Malformed discovery data (non-dict items, malformed graph)
- Integration flow: Discovery -> Graph -> Classification -> Composer
"""

import pytest

from agents.classification_agent import classification_agent_node
from agents.graph_agent import graph_agent_node
from agents.terraform_composer import terraform_composer_node
from models.adoption import ClassificationReport
from tools.graph_builder import DependencyGraphBuilder
from tools.resource_classifier import classify_resources


# ==============================================================================
# Unit Tests: classify_resources
# ==============================================================================

def test_classify_normal_managed_resource_by_tag():
    resources = [
        {
            "id": "vpc-managed-1",
            "resource_type": "aws_vpc",
            "name": "terraform-vpc",
            "cidr_block": "10.0.0.0/16",
            "tags": [{"Key": "ManagedBy", "Value": "Terraform"}],
        },
        {
            "id": "s3-managed-1",
            "resource_type": "aws_s3_bucket",
            "name": "iac-bucket",
            "tags": [{"Key": "terraform", "Value": "true"}],
        },
        {
            "id": "subnet-managed-1",
            "resource_type": "aws_subnet",
            "name": "cfn-subnet",
            "vpc_id": "vpc-managed-1",
            "tags": [{"Key": "aws:cloudformation:stack-name", "Value": "core-stack"}],
        },
    ]
    graph = {
        "nodes": [{"id": "vpc-managed-1"}, {"id": "s3-managed-1"}, {"id": "subnet-managed-1"}],
        "links": [{"source": "vpc-managed-1", "target": "subnet-managed-1"}],
    }

    report: ClassificationReport = classify_resources(resources, graph)

    assert report.summary["managed"] == 3
    for c in report.classifications:
        assert c.category == "managed"
        assert c.recommended_action == "skip"
        assert any("already managed" in r for r in c.reason)


def test_classify_normal_managed_resource_by_explicit_flag():
    resources = [
        {
            "id": "i-managed-1",
            "resource_type": "aws_instance",
            "is_managed": True,
            "tags": [],
        }
    ]
    graph = {"nodes": [{"id": "i-managed-1"}], "links": []}

    report = classify_resources(resources, graph)
    assert report.summary["managed"] == 1
    assert report.classifications[0].category == "managed"
    assert report.classifications[0].recommended_action == "skip"


def test_classify_unmanaged_resource_with_dependencies():
    resources = [
        {
            "id": "vpc-unmanaged-1",
            "resource_type": "aws_vpc",
            "name": "app-vpc",
            "cidr_block": "10.0.0.0/16",
            "tags": [{"Key": "Environment", "Value": "dev"}],
        },
        {
            "id": "subnet-unmanaged-1",
            "resource_type": "aws_subnet",
            "name": "app-subnet",
            "vpc_id": "vpc-unmanaged-1",
            "tags": [],
        },
    ]
    graph = {
        "nodes": [{"id": "vpc-unmanaged-1"}, {"id": "subnet-unmanaged-1"}],
        "links": [{"source": "vpc-unmanaged-1", "target": "subnet-unmanaged-1"}],
    }

    report = classify_resources(resources, graph)

    assert report.summary["unmanaged"] == 2
    for c in report.classifications:
        assert c.category == "unmanaged"
        assert c.recommended_action == "import"
        assert any("newly discovered" in r for r in c.reason)


def test_classify_shared_default_security_group():
    resources = [
        {
            "id": "sg-default-1",
            "resource_type": "aws_security_group",
            "name": "default",
            "group_name": "default",
            "vpc_id": "vpc-1",
            "tags": [],
        }
    ]
    graph = {"nodes": [{"id": "sg-default-1"}], "links": []}

    report = classify_resources(resources, graph)

    assert report.summary["shared"] == 1
    assert report.classifications[0].category == "shared"
    assert report.classifications[0].recommended_action == "data_source"


def test_classify_shared_default_vpc():
    resources = [
        {
            "id": "vpc-default-1",
            "resource_type": "aws_vpc",
            "is_default": True,
            "cidr_block": "172.31.0.0/16",
            "tags": [],
        }
    ]
    graph = {"nodes": [{"id": "vpc-default-1"}], "links": []}

    report = classify_resources(resources, graph)

    assert report.summary["shared"] == 1
    assert report.classifications[0].category == "shared"
    assert report.classifications[0].recommended_action == "data_source"


def test_classify_shared_service_linked_iam_role():
    resources = [
        {
            "id": "role-slr-1",
            "resource_type": "aws_iam_role",
            "name": "AWSServiceRoleForRDS",
            "arn": "arn:aws:iam::123456789012:role/aws-service-role/rds.amazonaws.com/AWSServiceRoleForRDS",
            "tags": [],
        }
    ]
    graph = {"nodes": [{"id": "role-slr-1"}], "links": []}

    report = classify_resources(resources, graph)

    assert report.summary["shared"] == 1
    assert report.classifications[0].category == "shared"
    assert report.classifications[0].recommended_action == "skip"


def test_classify_shared_by_explicit_tag():
    resources = [
        {
            "id": "subnet-shared-1",
            "resource_type": "aws_subnet",
            "name": "shared-dmz-subnet",
            "tags": [{"Key": "Shared", "Value": "true"}],
        }
    ]
    graph = {"nodes": [{"id": "subnet-shared-1"}], "links": []}

    report = classify_resources(resources, graph)

    assert report.summary["shared"] == 1
    assert report.classifications[0].category == "shared"
    assert report.classifications[0].recommended_action == "data_source"


def test_classify_orphaned_resource():
    resources = [
        {
            "id": "s3-orphan-1",
            "resource_type": "aws_s3_bucket",
            "name": "forgotten-bucket",
            "tags": [],
        }
    ]
    # In graph with 0 links -> degree 0
    graph = {
        "nodes": [{"id": "s3-orphan-1"}],
        "links": [],
    }

    report = classify_resources(resources, graph)

    assert report.summary["orphaned"] == 1
    assert report.classifications[0].category == "orphaned"
    assert report.classifications[0].recommended_action == "manual_review"
    assert any("no other discovered resource references" in r for r in report.classifications[0].reason)


def test_classify_orphaned_iam_role_unassumed_trust_policy():
    resources = [
        {
            "id": "role-orphan-1",
            "resource_type": "aws_iam_role",
            "name": "unused-ec2-role",
            "tags": [],
        }
    ]
    graph = {
        "nodes": [
            {
                "id": "role-orphan-1",
                "trusted_by_service_types": ["ec2.amazonaws.com"],
            }
        ],
        "links": [],
    }

    report = classify_resources(resources, graph)

    assert report.summary["orphaned"] == 1
    assert report.classifications[0].category == "orphaned"
    assert report.classifications[0].recommended_action == "manual_review"
    assert any("trusted by ec2.amazonaws.com but no matching resource" in r for r in report.classifications[0].reason)


def test_classify_unsupported_resource_type():
    resources = [
        {
            "id": "lambda-1",
            "resource_type": "aws_lambda_function",
            "name": "payment-handler",
            "tags": [],
        },
        {
            "id": "sqs-1",
            "resource_type": "aws_sqs_queue",
            "name": "order-queue",
            "tags": [],
        },
    ]
    graph = {"nodes": [{"id": "lambda-1"}, {"id": "sqs-1"}], "links": []}

    report = classify_resources(resources, graph)

    assert report.summary["unsupported"] == 2
    for c in report.classifications:
        assert c.category == "unsupported"
        assert c.recommended_action == "manual_review"
        assert any("no adoption support yet" in r for r in c.reason)


def test_classify_missing_metadata():
    resources = [
        # Missing id
        {"resource_type": "aws_vpc", "cidr_block": "10.0.0.0/16"},
        # Missing resource_type
        {"id": "res-no-type", "name": "unknown"},
        # Empty dict
        {},
    ]
    graph = {"nodes": [], "links": []}

    report = classify_resources(resources, graph)

    assert report.summary["unsupported"] == 3
    for c in report.classifications:
        assert c.category == "unsupported"
        assert c.recommended_action == "manual_review"


def test_classify_malformed_discovery_data():
    resources = [
        "not-a-dict",  # Malformed item
        None,          # None item
        12345,         # Number
    ]
    graph = "not-a-dict-graph"  # Malformed graph

    report = classify_resources(resources, graph)

    assert report.summary["unsupported"] == 3
    for c in report.classifications:
        assert c.category == "unsupported"
        assert c.recommended_action == "manual_review"


# ==============================================================================
# Integration Tests: Discovery -> Graph -> Classification -> Composer
# ==============================================================================

@pytest.mark.asyncio
async def test_pipeline_integration_discovery_to_composer():
    """Verifies that classification is authoritative:
    - unmanaged resources -> generate managed resource blocks
    - managed resources -> skipped
    - shared resources (default SG/VPC) -> generate data source blocks
    - shared resources (service role) -> skipped
    - orphaned resources -> skipped from resource generation
    - unsupported resources -> skipped from resource generation and explicitly reported
    """
    discovered_resources = [
        # 1. Unmanaged VPC & Subnet (connected) -> should be generated as resource blocks
        {
            "id": "vpc-app-1",
            "resource_type": "aws_vpc",
            "name": "prod-vpc",
            "cidr_block": "10.100.0.0/16",
            "tags": [{"Key": "Name", "Value": "prod-vpc"}],
        },
        {
            "id": "subnet-app-1",
            "resource_type": "aws_subnet",
            "name": "prod-subnet-1",
            "vpc_id": "vpc-app-1",
            "cidr_block": "10.100.1.0/24",
            "tags": [{"Key": "Name", "Value": "prod-subnet-1"}],
        },
        # 2. Already Managed EC2 instance -> should be skipped
        {
            "id": "i-managed-1",
            "resource_type": "aws_instance",
            "name": "legacy-tf-instance",
            "instance_type": "t3.micro",
            "vpc_id": "vpc-app-1",
            "subnet_id": "subnet-app-1",
            "tags": [{"Key": "ManagedBy", "Value": "Terraform"}],
        },
        # 3. Shared default security group -> should generate data block
        {
            "id": "sg-default-1",
            "resource_type": "aws_security_group",
            "name": "default",
            "group_name": "default",
            "vpc_id": "vpc-app-1",
            "tags": [],
        },
        # 4. Shared service-linked IAM role -> should be skipped
        {
            "id": "role-slr-1",
            "resource_type": "aws_iam_role",
            "name": "AWSServiceRoleForEC2",
            "arn": "arn:aws:iam::123456789012:role/aws-service-role/ec2.amazonaws.com/AWSServiceRoleForEC2",
            "tags": [],
        },
        # 5. Orphaned S3 bucket (zero edges in graph) -> should be skipped
        {
            "id": "s3-orphan-1",
            "resource_type": "aws_s3_bucket",
            "name": "standalone-orphan-bucket",
            "tags": [],
        },
        # 6. Unsupported resource type -> should be skipped & reported
        {
            "id": "kinesis-1",
            "resource_type": "aws_kinesis_stream",
            "name": "events-stream",
            "tags": [],
        },
    ]

    # Step 1: Graph Builder
    graph_builder = DependencyGraphBuilder()
    dep_graph = graph_builder.build_graph(discovered_resources)

    # Step 2: Classification Node
    classification_state = {
        "job_id": "test-job-classification",
        "resources": discovered_resources,
        "dependency_graph": dep_graph,
        "completed_agents": ["graph_agent"],
    }
    class_result = await classification_agent_node(classification_state)
    classification_results = class_result["classification_results"]

    # Verify classification summary
    summary = classification_results["summary"]
    assert summary["unmanaged"] == 2      # vpc-app-1, subnet-app-1
    assert summary["managed"] == 1        # i-managed-1
    assert summary["shared"] == 2         # sg-default-1, role-slr-1
    assert summary["orphaned"] == 1       # s3-orphan-1
    assert summary["unsupported"] == 1    # kinesis-1

    # Step 3: Terraform Composer Node
    composer_state = {
        "job_id": "test-job-classification",
        "region": "us-east-1",
        "resources": discovered_resources,
        "classification_results": classification_results,
        "dependency_graph": dep_graph,
        "completed_agents": class_result["completed_agents"],
    }
    composer_result = await terraform_composer_node(composer_state)
    tf_files = composer_result["terraform_files"]
    all_hcl = "\n".join(tf_files.values())

    # Assertions on generated HCL:
    # 1. Unmanaged VPC & Subnet ARE generated as managed resource blocks
    assert 'resource "aws_vpc" "prod_vpc_' in all_hcl
    assert 'resource "aws_subnet" "prod_subnet_1_' in all_hcl

    # 2. Managed instance is NOT generated
    assert 'resource "aws_instance"' not in all_hcl

    # 3. Shared default SG is generated as data source, NOT resource
    assert 'data "aws_security_group" "default_' in all_hcl
    assert 'resource "aws_security_group"' not in all_hcl

    # 4. Shared service-linked role is NOT generated
    assert 'resource "aws_iam_role"' not in all_hcl

    # 5. Orphaned S3 bucket is NOT generated as managed resource
    assert 'resource "aws_s3_bucket"' not in all_hcl

    # 6. Unsupported Kinesis stream is NOT generated
    assert 'aws_kinesis_stream' not in all_hcl
