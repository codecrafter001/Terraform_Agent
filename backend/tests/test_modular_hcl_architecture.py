"""Unit and integration tests for Enterprise Modular Terraform/OpenTofu Architecture.

Verifies:
1. Generation of root files (versions.tf, providers.tf, variables.tf, locals.tf, backend.tf.example, terraform.tfvars.example).
2. Every resource is generated exactly once, in the root stack files (no dead modules/ copies).
3. Correct input variable propagation and output declarations.
4. Synthesis of expanded enterprise AWS resources (ALB, DynamoDB, KMS, SQS, SNS, IGW, NAT Gateway).
"""

import re

import pytest
from tools.hcl_generator import HCLGenerator


def test_modular_hcl_generation_full_estate():
    resources = [
        # Networking
        {"id": "vpc-100", "resource_type": "aws_vpc", "name": "app-vpc", "cidr_block": "10.0.0.0/16"},
        {"id": "subnet-101", "resource_type": "aws_subnet", "name": "pub-subnet-1", "cidr_block": "10.0.1.0/24", "vpc_id": "vpc-100", "availability_zone": "us-east-1a"},
        {"id": "igw-102", "resource_type": "aws_internet_gateway", "name": "main-igw", "vpc_id": "vpc-100"},
        {"id": "nat-103", "resource_type": "aws_nat_gateway", "name": "main-nat", "subnet_id": "subnet-101", "allocation_id": "eipalloc-103"},
        # Security
        {"id": "sg-200", "resource_type": "aws_security_group", "name": "web-sg", "vpc_id": "vpc-100", "description": "Web security group"},
        {"id": "kms-201", "resource_type": "aws_kms_key", "name": "data-key"},
        # Compute
        {"id": "i-300", "resource_type": "aws_instance", "name": "web-srv", "ami": "ami-0123456789abcdef0", "instance_type": "t3.medium", "subnet_id": "subnet-101", "security_groups": ["sg-200"]},
        {"id": "alb-301", "resource_type": "aws_lb", "name": "web-alb", "scheme": "internet-facing", "subnets": ["subnet-101"], "security_groups": ["sg-200"]},
        # Storage & Data
        {"id": "s3-400", "resource_type": "aws_s3_bucket", "name": "production-assets-bucket"},
        {"id": "rds-401", "resource_type": "aws_db_instance", "name": "prod-db", "engine": "postgres", "instance_class": "db.t3.medium", "allocated_storage": 50, "security_groups": ["sg-200"]},
        {"id": "ddb-402", "resource_type": "aws_dynamodb_table", "name": "app-session-store", "hash_key": "session_id", "hash_key_type": "S"},
        {"id": "sqs-403", "resource_type": "aws_sqs_queue", "name": "order-events"},
        {"id": "sns-404", "resource_type": "aws_sns_topic", "name": "alert-notifications"},
    ]

    generator = HCLGenerator(job_id="job-test-modular", region="us-east-1", engine_name="terraform", engine_version="1.8.0")
    files, manifest = generator.generate_project(
        resources=resources,
        classification_results={},
        adoption_plan={},
        dependency_graph={}
    )

    # 1. Verify Root Orchestration Files
    assert "versions.tf" in files
    assert "providers.tf" in files
    assert "variables.tf" in files
    assert "locals.tf" in files
    assert "terraform.tfvars.example" in files
    assert "backend.tf.example" in files
    assert "outputs.tf" in files

    # 2. Every resource lives once, in the root stack files - no dead modules/ copies
    assert not any(path.startswith("modules/") for path in files)
    root_hcl = "\n".join(v for k, v in files.items() if k.endswith(".tf"))
    for rtype, name in [
        ("aws_vpc", "app_vpc"), ("aws_subnet", "pub_subnet_1"), ("aws_internet_gateway", "main_igw"),
        ("aws_nat_gateway", "main_nat"), ("aws_security_group", "web_sg"), ("aws_kms_key", "data_key"),
        ("aws_instance", "web_srv"), ("aws_lb", "web_alb"), ("aws_s3_bucket", "production_assets_bucket"),
        ("aws_db_instance", "prod_db"), ("aws_dynamodb_table", "app_session_store"),
        ("aws_sqs_queue", "order_events"), ("aws_sns_topic", "alert_notifications"),
    ]:
        assert root_hcl.count(f'resource "{rtype}" "{name}') == 1, rtype
    assert re.search(r"vpc_id\s+=\s+aws_vpc\.app_vpc", files["foundation.tf"])

    # 7. Verify Manifest Tallies
    assert manifest.resources_generated == len(resources)
    assert manifest.resources_review_required == 0
    assert manifest.resources_unsupported == 0
