"""Week 4: import {} blocks and the plan -generate-config-out cross-check."""

from typing import Any, Dict

import pytest

from tools.config_crosscheck import compare, top_level_literals
from tools.hcl_generator import HCLGenerator
from tools.import_blocks import ImportEntry, compose_imports_tf, filter_imports_tf, import_targets
from tools.resource_classifier import classify_resources

# --- composing imports.tf ---------------------------------------------------


def test_imports_tf_follows_adoption_order_then_address():
    entries = [
        ImportEntry("subnet-1", "aws_subnet.a", "subnet-1"),
        ImportEntry("vpc-1", "aws_vpc.main", "vpc-1"),
        ImportEntry("sg-1", "aws_security_group.web", "sg-1"),
    ]
    text = compose_imports_tf(entries, import_order=["vpc-1", "subnet-1"])
    assert list(import_targets(text)) == ["aws_vpc.main", "aws_subnet.a", "aws_security_group.web"]
    assert "never runs apply or import" in text


def test_import_ids_are_escaped_as_hcl_strings():
    text = compose_imports_tf([ImportEntry("q", "aws_sqs_queue.q", 'https://sqs/q"x')])
    assert import_targets(text) == {"aws_sqs_queue.q": 'https://sqs/q"x'}


def test_filter_keeps_only_requested_addresses_in_order():
    entries = [ImportEntry(a, a, a.split(".")[1]) for a in ("aws_vpc.a", "aws_subnet.b", "aws_instance.c")]
    text = compose_imports_tf(entries, import_order=["aws_vpc.a", "aws_subnet.b", "aws_instance.c"])
    wave = filter_imports_tf(text, ["aws_instance.c", "aws_vpc.a"])
    # The file's (dependency-safe) order is preserved, not the request order.
    assert list(import_targets(wave)) == ["aws_vpc.a", "aws_instance.c"]
    assert filter_imports_tf(text, ["aws_s3_bucket.none"]) is None


# --- generator emits imports.tf for managed resources only -------------------

RESOURCES = [
    {"id": "vpc-0abc", "resource_type": "aws_vpc", "name": "main", "cidr_block": "10.0.0.0/16", "tags": []},
    {"id": "subnet-0def", "resource_type": "aws_subnet", "name": "a", "vpc_id": "vpc-0abc",
     "cidr_block": "10.0.1.0/24", "availability_zone": "us-east-1a", "tags": []},
    {"id": "vpc-shared", "resource_type": "aws_vpc", "name": "shared", "cidr_block": "10.9.0.0/16",
     "tags": [{"Key": "ManagedBy", "Value": "terraform"}]},
    {"id": "app-role", "resource_type": "aws_iam_role", "name": "app-role", "tags": []},
]
GRAPH = {
    "nodes": [{"id": r["id"]} for r in RESOURCES],
    "links": [{"source": "vpc-0abc", "target": "subnet-0def"}, {"source": "app-role", "target": "subnet-0def"},
              {"source": "vpc-shared", "target": "subnet-0def"}],
    "stacks": [],
}


def _generate():
    from tools.adoption_planner import build_adoption_plan

    classification = classify_resources(RESOURCES, GRAPH).model_dump()
    plan = build_adoption_plan(classification, GRAPH, RESOURCES).model_dump()
    gen = HCLGenerator(job_id="job-w4", region="us-east-1")
    return gen.generate_project(RESOURCES, classification, plan, GRAPH)


def test_generator_writes_import_blocks_for_managed_resources_only():
    files, manifest = _generate()
    targets = import_targets(files["imports.tf"])
    ids = set(targets.values())

    assert {"vpc-0abc", "subnet-0def"} <= ids  # managed
    assert "vpc-shared" not in ids  # reference -> data block, never imported
    assert "app-role" not in ids  # review
    # Every import target exists as a root resource block.
    root = "\n".join(c for n, c in files.items() if "/" not in n)
    for address in targets:
        rtype, name = address.split(".")
        assert f'resource "{rtype}" "{name}"' in root
    assert "imports.tf" in manifest.generated_files


# --- cross-check ------------------------------------------------------------

OURS = '''resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  instance_tenancy     = "default"
  tags = {
    Name = "main"
  }
}

resource "aws_subnet" "a" {
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.0.1.0/24"
}
'''
GENERATED = '''# __generated__ by Terraform from "vpc-0abc"
resource "aws_vpc" "main" {
  assign_generated_ipv6_cidr_block = false
  cidr_block                       = "10.0.0.0/16"
  enable_dns_hostnames             = false
  instance_tenancy                 = "default"
  tags = {
    Name = "main"
  }
}

resource "aws_subnet" "a" {
  vpc_id     = "vpc-0abc"
  cidr_block = "10.0.1.0/24"
}
'''


def test_top_level_literals_skip_references_and_nested_blocks():
    body = OURS.split("{", 1)[1]
    assert top_level_literals(body) == {"cidr_block": '"10.0.0.0/16"', "enable_dns_hostnames": "true",
                                        "instance_tenancy": '"default"'}


def test_compare_reports_literal_mismatches_only():
    result = compare({"foundation.tf": OURS}, GENERATED)
    assert result["resources_checked"] == 2
    assert result["mismatches"] == [{
        "address": "aws_vpc.main", "attribute": "enable_dns_hostnames",
        "generated_by_terraagent": "true", "live_per_terraform": "false",
    }]  # vpc_id reference vs literal is deliberately not compared


def test_compare_ignores_module_copies_and_flags_missing_targets():
    result = compare({"foundation.tf": OURS, "modules/networking/main.tf": OURS}, GENERATED.split("resource \"aws_subnet\"")[0],
                     ["aws_vpc.main", "aws_subnet.a"])
    assert result["resources_missing_from_terraform_output"] == ["aws_subnet.a"]


# --- cross-check step -------------------------------------------------------

import agents.config_crosscheck_agent as cc  # noqa: E402


@pytest.fixture
def quiet(monkeypatch):
    async def publish(*a, **k):
        return None
    monkeypatch.setattr(cc.redis_service, "publish_log", publish)


def _state(**extra) -> Dict[str, Any]:
    files = {"foundation.tf": OURS,
             "imports.tf": compose_imports_tf([ImportEntry("vpc-0abc", "aws_vpc.main", "vpc-0abc")])}
    return {"job_id": "j", "terraform_files": files, "run_plan_equivalence": True,
            "aws_credentials": {"access_key": "AKIAFAKE", "secret_key": "s"}, **extra}


@pytest.mark.parametrize("override,reason", [
    ({"run_plan_equivalence": False}, "opt-in"),
    ({"aws_credentials": {}}, "credentials"),
    ({"terraform_files": {"foundation.tf": OURS}}, "no import blocks"),
])
async def test_crosscheck_skips_cleanly(quiet, override, reason):
    out = await cc.config_crosscheck_node(_state(**override))
    assert out["config_crosscheck"]["skipped"] and reason in out["config_crosscheck"]["reason"]


async def test_crosscheck_reports_mismatches(monkeypatch, quiet):
    async def fake(files, creds, region, binary):
        return {"passed": True, "generated": GENERATED, "checks": []}
    monkeypatch.setattr(cc.TerraformRunner, "generate_config", staticmethod(fake))

    out = (await cc.config_crosscheck_node(_state()))["config_crosscheck"]

    assert out["skipped"] is False and out["resources_checked"] == 1
    assert out["mismatches"][0]["attribute"] == "enable_dns_hostnames"


async def test_crosscheck_failure_is_recorded_for_fail_closed(monkeypatch, quiet):
    async def fake(files, creds, region, binary):
        return {"passed": False, "generated": "", "checks": [{"check_name": "init", "passed": False}]}
    monkeypatch.setattr(cc.TerraformRunner, "generate_config", staticmethod(fake))

    out = (await cc.config_crosscheck_node(_state()))["config_crosscheck"]

    assert out["error"]
    from agents.graph import _incomplete_reasons
    assert any("cross-check could not run" in r for r in _incomplete_reasons({"config_crosscheck": out}))


def test_generate_config_never_passes_mutating_verbs(monkeypatch):
    # Goes through TerraformRunner.run_command's argv guard like every terraform call.
    import inspect

    from tools.terraform_runner import TerraformRunner
    src = inspect.getsource(TerraformRunner.generate_config)
    assert "cls.run_command(" in src and "create_subprocess_exec" not in src
    assert '"plan"' in src and '"apply"' not in src and '"import"' not in src


# --- wave PRs ---------------------------------------------------------------

def test_wave_pr_imports_only_target_that_wave():
    from services.github_client import _extract_wave_files
    from tools.naming import unique_clean_name

    files, _ = _generate()
    by_id = {r["id"]: r for r in RESOURCES}
    wave = _extract_wave_files(files, ["subnet-0def"], by_id)

    targets = import_targets(wave["imports.tf"])
    assert list(targets) == [f"aws_subnet.{unique_clean_name('a', 'subnet-0def')}"]
