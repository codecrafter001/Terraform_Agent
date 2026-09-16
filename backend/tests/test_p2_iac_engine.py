"""Comprehensive tests for Phase 2: Production Terraform/OpenTofu Engine."""

import os
import pytest
from unittest.mock import AsyncMock, patch

from agents.terraform_composer import terraform_composer_node
from agents.validation_agent import validation_agent_node
from models.manifest import GenerationManifest
from tools.hcl_generator import HCLGenerator
from tools.iac_engine import (
    IaCEngine,
    OpenTofuEngine,
    TerraformEngine,
    get_iac_engine,
)
from tools.terraform_runner import TerraformRunner


# =========================================================================
# 1. Engine Abstraction Tests
# =========================================================================

def test_iac_engine_factory():
    """Test get_iac_engine for valid and invalid engines."""
    tf_engine = get_iac_engine("terraform")
    assert isinstance(tf_engine, TerraformEngine)
    assert tf_engine.name == "terraform"
    assert tf_engine.display_name == "Terraform"

    tofu_engine = get_iac_engine("tofu")
    assert isinstance(tofu_engine, OpenTofuEngine)
    assert tofu_engine.name == "opentofu"
    assert tofu_engine.display_name == "OpenTofu"

    # Default fallback
    default_engine = get_iac_engine(None)
    assert isinstance(default_engine, TerraformEngine)

    # Invalid engine raises ValueError
    with pytest.raises(ValueError, match="Unsupported IaC engine"):
        get_iac_engine("cloudformation")

    with pytest.raises(ValueError, match="Unsupported IaC engine"):
        get_iac_engine("pulumi")


def test_iac_engine_command_construction():
    """Test engine CLI command construction."""
    tf = TerraformEngine(binary_path="/usr/bin/terraform")
    assert tf.build_fmt_command() == ["/usr/bin/terraform", "fmt", "-check", "-diff"]
    assert tf.build_init_command(backend=False) == ["/usr/bin/terraform", "init", "-backend=false"]
    assert tf.build_validate_command() == ["/usr/bin/terraform", "validate", "-json"]

    tofu = OpenTofuEngine(binary_path="/usr/bin/tofu")
    assert tofu.build_fmt_command() == ["/usr/bin/tofu", "fmt", "-check", "-diff"]
    assert tofu.build_init_command(backend=False) == ["/usr/bin/tofu", "init", "-backend=false"]
    assert tofu.build_validate_command() == ["/usr/bin/tofu", "validate", "-json"]


# =========================================================================
# 2. Safety Guardrail Tests
# =========================================================================

@pytest.mark.asyncio
async def test_disallowed_commands_blocked_in_engine():
    """Verify that apply, destroy, and import are hard-blocked by IaCEngine and TerraformRunner."""
    tf_engine = get_iac_engine("terraform")

    with pytest.raises(ValueError, match="Safety Violation"):
        await tf_engine.run_command(["apply"])

    with pytest.raises(ValueError, match="Safety Violation"):
        await tf_engine.run_command(["destroy"])

    with pytest.raises(ValueError, match="Safety Violation"):
        await tf_engine.run_command(["import"])

    with pytest.raises(ValueError, match="Safety Violation"):
        await TerraformRunner.run_command(["apply"], cwd="/tmp")


# =========================================================================
# 3. Deterministic & Dependency-Aware HCL Generation Tests
# =========================================================================

def test_hcl_generator_no_fake_defaults_and_dependency_references():
    """Verify that missing required attributes trigger review_required with unresolved attributes,
    and cross-resource dependencies emit Terraform references instead of literal IDs."""
    from tools.naming import unique_clean_name

    resources = [
        {
            "id": "vpc-001",
            "resource_type": "aws_vpc",
            "name": "production-vpc",
            "cidr_block": "10.0.0.0/16",
            "tags": [{"Key": "Environment", "Value": "Production"}]
        },
        {
            "id": "subnet-001",
            "resource_type": "aws_subnet",
            "name": "app-subnet",
            "cidr_block": "10.0.1.0/24",
            "vpc_id": "vpc-001",
            "availability_zone": "us-east-1a"
        },
        {
            "id": "sg-001",
            "resource_type": "aws_security_group",
            "name": "web-sg",
            "description": "Web traffic SG",
            "vpc_id": "vpc-001",
            "ip_permissions": [
                {
                    "FromPort": 443,
                    "ToPort": 443,
                    "IpProtocol": "tcp",
                    "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTPS"}]
                }
            ]
        },
        {
            "id": "i-001",
            "resource_type": "aws_instance",
            "name": "web-server",
            "instance_type": "t3.medium",
            "ami": "ami-12345678",
            "subnet_id": "subnet-001",
            "security_groups": ["sg-001"]
        },
        # Resource missing required attribute (VPC without CIDR)
        {
            "id": "vpc-broken",
            "resource_type": "aws_vpc",
            "name": "broken-vpc"
            # Missing cidr_block!
        }
    ]

    adoption_plan = {
        "categories": [
            {"category": "safe_to_import", "resource_ids": ["vpc-001", "subnet-001", "sg-001", "i-001", "vpc-broken"]}
        ]
    }

    generator = HCLGenerator(
        job_id="job-p2-test",
        region="us-east-1",
        engine_name="terraform"
    )

    files, manifest = generator.generate_project(
        resources=resources,
        classification_results={},
        adoption_plan=adoption_plan,
        dependency_graph={}
    )

    # 1. Check generated files
    assert "foundation.tf" in files
    assert "security.tf" in files
    assert "application.tf" in files
    assert "versions.tf" in files
    assert "providers.tf" in files

    foundation_hcl = files["foundation.tf"]
    security_hcl = files["security.tf"]
    app_hcl = files["application.tf"]

    # 2. Dependency reference checks:
    vpc_clean = unique_clean_name("production-vpc", "vpc-001")
    subnet_clean = unique_clean_name("app-subnet", "subnet-001")
    sg_clean = unique_clean_name("web-sg", "sg-001")

    # Subnet should reference aws_vpc.<clean>.id, NOT literal "vpc-001"
    assert f"aws_vpc.{vpc_clean}.id" in foundation_hcl

    # Security Group should reference aws_vpc.<clean>.id
    assert f"aws_vpc.{vpc_clean}.id" in security_hcl

    # EC2 instance should reference subnet and security group
    assert f"aws_subnet.{subnet_clean}.id" in app_hcl
    assert f"aws_security_group.{sg_clean}.id" in app_hcl

    # 3. No fake defaults check:
    # vpc-broken was missing cidr_block, so it should be flagged as review_required in unresolved_attributes
    assert manifest.job_id == "job-p2-test"
    assert manifest.engine == "terraform"
    assert len(manifest.unresolved_attributes) > 0
    broken_unresolved = [u for u in manifest.unresolved_attributes if u.resource_id == "vpc-broken"]
    assert len(broken_unresolved) == 1
    assert broken_unresolved[0].attribute_name == "cidr_block"
    assert manifest.adoption_outcomes.get("vpc-broken") == "review_required"


def test_hcl_generator_adoption_plan_compliance():
    """Verify that safe_to_import, use_data_source, do_not_manage, and unsupported are strictly respected."""
    resources = [
        {"id": "vpc-safe", "resource_type": "aws_vpc", "name": "safe-vpc", "cidr_block": "10.0.0.0/16"},
        {"id": "sg-shared", "resource_type": "aws_security_group", "name": "shared-sg"},
        {"id": "role-unmanaged", "resource_type": "aws_iam_role", "name": "aws-service-role"},
        {"id": "custom-unsupported", "resource_type": "aws_future_service", "name": "future-res"},
    ]

    adoption_plan = {
        "categories": [
            {"category": "safe_to_import", "resource_ids": ["vpc-safe"]},
            {"category": "use_data_source", "resource_ids": ["sg-shared"]},
            {"category": "do_not_manage", "resource_ids": ["role-unmanaged"]},
            {"category": "unsupported", "resource_ids": ["custom-unsupported"]},
        ]
    }

    generator = HCLGenerator(
        job_id="job-plan-test",
        region="us-west-2",
        engine_name="opentofu"
    )

    files, manifest = generator.generate_project(
        resources=resources,
        classification_results={},
        adoption_plan=adoption_plan,
        dependency_graph={}
    )
    all_hcl = "\n".join(files.values())

    # 1. safe_to_import generates managed resource
    assert 'resource "aws_vpc"' in all_hcl

    # 2. use_data_source generates data source
    assert 'data "aws_security_group"' in all_hcl
    assert 'resource "aws_security_group"' not in all_hcl

    # 3. do_not_manage generates nothing
    assert "aws-service-role" not in all_hcl
    assert 'resource "aws_iam_role"' not in all_hcl

    # 4. unsupported is omitted and counted
    assert "future-res" not in all_hcl
    assert manifest.resources_unsupported == 1
    assert manifest.resources_skipped == 1
    assert manifest.engine == "opentofu"


# =========================================================================
# 4. Security / Zero Secrets in Generated Files
# =========================================================================

def test_no_secrets_in_generated_files():
    """Ensure generated HCL files do not contain hardcoded AWS secret keys, access keys, or session tokens."""
    resources = [
        {"id": "s3-bucket-1", "resource_type": "aws_s3_bucket", "name": "my-test-bucket-secure"},
    ]

    generator = HCLGenerator(
        job_id="job-sec-test",
        region="us-east-1",
        engine_name="terraform"
    )

    files, manifest = generator.generate_project(
        resources=resources,
        classification_results={},
        adoption_plan={"categories": [{"category": "safe_to_import", "resource_ids": ["s3-bucket-1"]}]},
        dependency_graph={}
    )

    secret_patterns = [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "access_key",
        "secret_key",
        "token",
    ]

    for filename, content in files.items():
        for pattern in secret_patterns:
            assert f"{pattern} =" not in content, f"Secret pattern '{pattern}' found in {filename}"


# =========================================================================
# 5. Composer Agent Node Integration Test
# =========================================================================

@pytest.mark.asyncio
async def test_terraform_composer_node_full():
    """Test terraform_composer_node full execution with engine and manifest propagation."""
    state = {
        "job_id": "job-comp-1",
        "region": "us-east-1",
        "terraform_binary": "tofu",
        "resources": [
            {"id": "vpc-1", "resource_type": "aws_vpc", "name": "main-vpc", "cidr_block": "10.0.0.0/16"},
            {"id": "s3-1", "resource_type": "aws_s3_bucket", "name": "assets-bucket"},
        ],
        "adoption_plan": {
            "categories": [
                {"category": "safe_to_import", "resource_ids": ["vpc-1", "s3-1"]},
            ]
        },
        "dependency_graph": {},
        "completed_agents": [],
    }

    result = await terraform_composer_node(state)

    assert result["current_agent"] == "validation_agent"
    assert "terraform_composer" in result["completed_agents"]
    assert "terraform_files" in result
    assert "generation_manifest" in result

    manifest = result["generation_manifest"]
    assert manifest["engine"] == "opentofu"
    assert manifest["resources_generated"] == 2
    assert manifest["resources_discovered"] == 2
    assert "foundation.tf" in result["terraform_files"]
    assert "data.tf" in result["terraform_files"]


# =========================================================================
# 6. Validation Agent Granular Status Tests
# =========================================================================

@pytest.mark.asyncio
async def test_validation_agent_status_pass():
    """Test validation agent marking status PASS when fmt, init, and validate all pass."""
    state = {
        "job_id": "job-val-pass",
        "region": "us-east-1",
        "terraform_binary": "terraform",
        "terraform_files": {
            "main.tf": 'resource "aws_vpc" "main" { cidr_block = "10.0.0.0/16" }'
        },
        "generation_manifest": {
            "job_id": "job-val-pass",
            "engine": "terraform",
            "validation_status": "PENDING"
        },
        "completed_agents": ["terraform_composer"],
    }

    # Mock TerraformRunner commands to simulate successful validation
    with patch("tools.terraform_runner.TerraformRunner.run_command") as mock_run:
        # Mock fmt, init, validate
        mock_run.side_effect = [
            (0, "", ""),                      # fmt
            (0, "Terraform initialized!", ""), # init
            (0, '{"valid": true, "error_count": 0, "warning_count": 0, "diagnostics": []}', "") # validate
        ]

        result = await validation_agent_node(state)

        assert result["validation_results"]["passed"] is True
        assert result["validation_results"]["validation_status"] == "PASS"
        assert result["generation_manifest"]["validation_status"] == "PASS"
        assert result["current_agent"] == "policy_agent"


@pytest.mark.asyncio
async def test_validation_agent_status_partial_and_fail():
    """Test validation agent marking status PARTIAL when some checks fail, and FAIL when all fail."""
    state = {
        "job_id": "job-val-fail",
        "region": "us-east-1",
        "terraform_binary": "terraform",
        "terraform_files": {
            "main.tf": 'resource "aws_vpc" "main" { invalid syntax'
        },
        "generation_manifest": {
            "job_id": "job-val-fail",
            "engine": "terraform",
            "validation_status": "PENDING"
        },
        "completed_agents": ["terraform_composer"],
    }

    # Case 1: Partial failure (init succeeds, fmt/validate fail -> PARTIAL)
    with patch("tools.terraform_runner.TerraformRunner.run_command") as mock_run:
        mock_run.side_effect = [
            (2, "main.tf", "syntax error"),  # fmt fails
            (0, "Initialized", ""),           # init passes
            (1, '{"valid": false, "error_count": 1, "diagnostics": [{"severity": "error", "summary": "syntax error"}]}', "error") # validate fails
        ]

        result = await validation_agent_node(state)

        assert result["validation_results"]["passed"] is False
        assert result["validation_results"]["validation_status"] == "PARTIAL"
        assert result["generation_manifest"]["validation_status"] == "PARTIAL"

    # Case 2: Complete failure (fmt fails, init fails -> FAIL)
    with patch("tools.terraform_runner.TerraformRunner.run_command") as mock_run:
        mock_run.side_effect = [
            (2, "main.tf", "syntax error"),      # fmt fails
            (1, "", "Failed to initialize"),     # init fails
        ]

        result = await validation_agent_node(state)

        assert result["validation_results"]["passed"] is False
        assert result["validation_results"]["validation_status"] == "FAIL"
        assert result["generation_manifest"]["validation_status"] == "FAIL"
        assert result["current_agent"] == "policy_agent"
