"""The Hardening proposal: explained fixes only where discovery shows the
setting is missing, never merged into the adoption code, validated or dropped;
cost is only estimated for it."""

import pytest

import agents.cost_agent as cost_mod
import agents.hardening_agent as hardening_mod
from tools.hardening import propose
from tools.hcl_blocks import extract_resource_block
from tools.hcl_generator import HCLGenerator
from tools.naming import unique_clean_name

ALL_ON = {"BlockPublicAcls": True, "BlockPublicPolicy": True, "IgnorePublicAcls": True, "RestrictPublicBuckets": True}


def _adoption(resources):
    gen = HCLGenerator(job_id="job-h", region="us-east-1", engine_name="terraform", engine_version="1.8.0")
    files, _ = gen.generate_project(resources=resources, classification_results={}, adoption_plan={},
                                    dependency_graph={})
    return files


BUCKET_OPEN = {"id": "open-bucket", "resource_type": "aws_s3_bucket", "name": "open-bucket",
               "public_access_block": {}, "encryption_rules": []}
BUCKET_OK = {"id": "ok-bucket", "resource_type": "aws_s3_bucket", "name": "ok-bucket",
             "public_access_block": ALL_ON, "encryption_rules": [{"x": 1}]}
BUCKET_UNKNOWN = {"id": "unknown-bucket", "resource_type": "aws_s3_bucket", "name": "unknown-bucket",
                  "public_access_block": None, "encryption_rules": None}
INSTANCE = {"id": "i-1", "resource_type": "aws_instance", "name": "web", "ami": "ami-1",
            "instance_type": "t3.micro", "metadata_http_tokens": "optional"}


def _addr(res):
    return f"{res['resource_type']}.{unique_clean_name(res['name'], res['id'])}"


def test_fixes_only_what_is_missing_and_leaves_adoption_untouched():
    bucket, web = _addr(BUCKET_OPEN), _addr(INSTANCE)
    resources = [BUCKET_OPEN, BUCKET_OK, BUCKET_UNKNOWN, INSTANCE]
    adoption = _adoption(resources)
    before = dict(adoption)
    findings = [
        {"tool": "checkov", "rule_id": "CKV_AWS_53", "severity": "HIGH",
         "resource": bucket, "description": "Ensure S3 bucket has block public ACLS enabled"},
        {"tool": "checkov", "rule_id": "CKV_AWS_79", "severity": "HIGH",
         "resource": web, "description": "Ensure Instance Metadata Service Version 1 is not enabled"},
        {"tool": "trivy", "rule_id": "AVD-AWS-0999", "severity": "CRITICAL",
         "resource": "aws_db_instance.x", "description": "Something with no automatic fix"},
    ]
    p = propose(adoption, resources, ["open-bucket", "ok-bucket", "unknown-bucket", "i-1"],
                {"findings": findings})

    assert adoption == before  # adoption code is never modified
    kinds = sorted((c["kind"], c["resource"]) for c in p["changes"])
    assert kinds == [("ec2_imdsv2", web), ("s3_encryption", bucket), ("s3_public_access_block", bucket)]
    assert all(c["explanation"] and c["impact"] in ("safe", "behavior_changing") for c in p["changes"])
    hardened = {**adoption, **p["files"]}
    assert 'http_tokens   = "required"' in extract_resource_block(
        "\n".join(hardened.values()), "aws_instance", web.split(".", 1)[1])
    # the two findings the changes resolve aren't repeated as manual recommendations
    assert [r["rule_id"] for r in p["recommendations"]] == ["AVD-AWS-0999"]


def test_unmanaged_resources_are_not_hardened():
    adoption = _adoption([BUCKET_OPEN])
    assert propose(adoption, [BUCKET_OPEN], [], {})["changes"] == []


@pytest.fixture
def quiet(monkeypatch):
    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(hardening_mod.redis_service, "publish_log", nothing)
    monkeypatch.setattr(cost_mod.redis_service, "publish_log", nothing)


class FakeEngine:
    def __init__(self, passed):
        self.passed = passed

    async def format_hcl(self, files):
        return files

    async def validate_hcl(self, files):
        return {"passed": self.passed, "checks": [] if self.passed else [{"passed": False, "output": "boom"}]}


def _state():
    return {
        "job_id": "job-h", "terraform_files": _adoption([BUCKET_OPEN]), "resources": [BUCKET_OPEN],
        "classification_results": {"classifications": [{"resource_id": "open-bucket", "decision": "manage"}]},
        "security_results": {"findings": []},
    }


async def test_validated_hardening_ships_files(monkeypatch, quiet):
    monkeypatch.setattr(hardening_mod, "get_iac_engine", lambda *_: FakeEngine(True))
    out = await hardening_mod.hardening_agent_node(_state())
    assert out["hardening"]["validated"] is True and out["hardening"]["files"]


async def test_hardening_that_fails_validation_ships_no_files(monkeypatch, quiet):
    monkeypatch.setattr(hardening_mod, "get_iac_engine", lambda *_: FakeEngine(False))
    out = await hardening_mod.hardening_agent_node(_state())
    h = out["hardening"]
    assert h["files"] == {} and h["changes"] and "validate failed" in h["rejected_reason"]


async def test_no_cost_estimate_without_hardening(quiet):
    out = await cost_mod.cost_agent_node({"job_id": "j", "terraform_files": {"a.tf": ""}, "hardening": {}})
    assert out["cost_results"]["skipped"] is True


async def test_cost_delta_for_hardening(monkeypatch, quiet):
    async def estimate(files):
        return {"total_monthly_cost": 10.0 + len(files), "currency": "USD", "resources": [], "tool_skipped": False}
    monkeypatch.setattr(cost_mod.InfracostRunner, "estimate_cost", staticmethod(estimate))
    out = await cost_mod.cost_agent_node({"job_id": "j", "terraform_files": {"a.tf": ""},
                                          "hardening": {"files": {"b.tf": ""}}})
    assert out["cost_results"]["monthly_delta"] == 1.0
    assert out["hardening"]["cost"]["monthly_delta"] == 1.0


async def test_missing_infracost_is_not_zero(monkeypatch, quiet):
    async def estimate(files):
        return {"total_monthly_cost": 0.0, "tool_skipped": True, "resources": []}
    monkeypatch.setattr(cost_mod.InfracostRunner, "estimate_cost", staticmethod(estimate))
    out = await cost_mod.cost_agent_node({"job_id": "j", "terraform_files": {}, "hardening": {"files": {"b.tf": ""}}})
    assert out["cost_results"]["monthly_delta"] is None and out["cost_results"]["tool_skipped"]


def test_block_extraction_ignores_braces_in_strings():
    content = ('resource "aws_vpc" "a" {\n  tags = {\n    "k" = "x}\\"{"\n  }\n}\n'
               'resource "aws_vpc" "b" {\n}\n')
    block = extract_resource_block(content, "aws_vpc", "a")
    assert block.endswith("  }\n}") and '"aws_vpc" "b"' not in block
