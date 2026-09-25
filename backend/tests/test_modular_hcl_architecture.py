"""Unit and integration tests for Enterprise Modular Terraform/OpenTofu Architecture.

Verifies:
1. Generation of root files (versions.tf, providers.tf, variables.tf, locals.tf, backend.tf.example, terraform.tfvars.example).
2. Generation of encapsulated submodules (modules/networking, modules/security, modules/compute, modules/storage_and_data).
3. Correct input variable propagation and output declarations.
4. Synthesis of expanded enterprise AWS resources (ALB, DynamoDB, KMS, SQS, SNS, IGW, NAT Gateway).
"""

import pytest
from tools.hcl_generator import HCLGenerator


def test_modular_hcl_generation_full_estate():
    resources = [
        # Networking
        {"id": "vpc-100", "resource_type": "aws_vpc", "name": "app-vpc", "cidr_block": "10.0.0.0/16"},
        {"id": "subnet-101", "resource_type": "aws_subnet", "name": "pub-subnet-1", "cidr_block": "10.0.1.0/24", "vpc_id": "vpc-100", "availability_zone": "us-east-1a"},
        {"id": "igw-102", "resource_type": "aws_internet_gateway", "name": "main-igw", "vpc_id": "vpc-100"},
        {"id": "nat-103", "resource_type": "aws_nat_gateway", "name": "main-nat", "subnet_id": "subnet-101"},
        # Security
        {"id": "sg-200", "resource_type": "aws_security_group", "name": "web-sg", "vpc_id": "vpc-100", "description": "Web security group"},
        {"id": "kms-201", "resource_type": "aws_kms_key", "name": "data-key"},
        # Compute
        {"id": "i-300", "resource_type": "aws_instance", "name": "web-srv", "ami": "ami-0123456789abcdef0", "instance_type": "t3.medium", "subnet_id": "subnet-101", "security_groups": ["sg-200"]},
        {"id": "alb-301", "resource_type": "aws_lb", "name": "web-alb", "scheme": "internet-facing", "subnets": ["subnet-101"], "security_groups": ["sg-200"]},
        # Storage & Data
        {"id": "s3-400", "resource_type": "aws_s3_bucket", "name": "production-assets-bucket"},
        {"id": "rds-401", "resource_type": "aws_db_instance", "name": "prod-db", "engine": "postgres", "instance_class": "db.t3.medium", "allocated_storage": 50, "security_groups": ["sg-200"]},
        {"id": "ddb-402", "resource_type": "aws_dynamodb_table", "name": "app-session-store", "hash_key": "session_id"},
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

    # 2. Verify Submodules Hierarchy
    assert "modules/networking/main.tf" in files
    assert "modules/networking/variables.tf" in files
    assert "modules/networking/outputs.tf" in files

    assert "modules/security/main.tf" in files
    assert "modules/security/variables.tf" in files
    assert "modules/security/outputs.tf" in files

    assert "modules/compute/main.tf" in files
    assert "modules/compute/variables.tf" in files
    assert "modules/compute/outputs.tf" in files

    assert "modules/storage_and_data/main.tf" in files
    assert "modules/storage_and_data/variables.tf" in files
    assert "modules/storage_and_data/outputs.tf" in files

    # 3. Verify Networking Module Content
    net_hcl = files["modules/networking/main.tf"]
    assert 'resource "aws_vpc" "app_vpc' in net_hcl
    assert 'resource "aws_subnet" "pub_subnet_1' in net_hcl
    assert 'resource "aws_internet_gateway" "main_igw' in net_hcl
    assert 'resource "aws_nat_gateway" "main_nat' in net_hcl
    assert "vpc_id                  = aws_vpc.app_vpc" in net_hcl

    # 4. Verify Security Module Content
    sec_hcl = files["modules/security/main.tf"]
    assert 'resource "aws_security_group" "web_sg' in sec_hcl
    assert 'resource "aws_kms_key" "data_key' in sec_hcl

    # 5. Verify Compute Module Content
    comp_hcl = files["modules/compute/main.tf"]
    assert 'resource "aws_instance" "web_srv' in comp_hcl
    assert 'resource "aws_lb" "web_alb' in comp_hcl

    # 6. Verify Storage & Data Module Content
    data_hcl = files["modules/storage_and_data/main.tf"]
    assert 'resource "aws_s3_bucket" "production_assets_bucket' in data_hcl
    assert 'resource "aws_db_instance" "prod_db' in data_hcl
    assert 'resource "aws_dynamodb_table" "app_session_store' in data_hcl
    assert 'resource "aws_sqs_queue" "order_events' in data_hcl
    assert 'resource "aws_sns_topic" "alert_notifications' in data_hcl

    # 7. Verify Manifest Tallies
    assert manifest.resources_generated == len(resources)
    assert manifest.resources_review_required == 0
    assert manifest.resources_unsupported == 0
