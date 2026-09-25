"""Week 2: invariant checks after repair, validation-driven repair, and
fail-closed security scanning."""

import json
import sys
from typing import Any, Dict, List

import pytest

from tools.hcl_invariants import addresses, check_repair_invariants
from tools.validation_diagnostics import enclosing_block, parse_diagnostics

VPC = 'resource "aws_vpc" "main" {\n  cidr_block = "10.0.0.0/16"\n  enable_dns_support = true\n}\n'
SUBNET = 'resource "aws_subnet" "a" {\n  vpc_id     = aws_vpc.main.id\n  cidr_block = "10.0.1.0/24"\n}\n'
FILES = {"foundation.tf": VPC + "\n" + SUBNET}


# --- invariants -------------------------------------------------------------

def test_addresses_cover_resources_data_and_modules():
    files = {"a.tf": VPC, "b.tf": 'data "aws_ami" "x" {\n}\nmodule "net" {\n  source = "./m"\n}\n'}
    assert addresses(files) == {"aws_vpc.main", "data.aws_ami.x", "module.net"}


def test_a_value_preserving_fix_passes():
    after = {"foundation.tf": FILES["foundation.tf"].replace("enable_dns_support", "enable_dns_hostnames")}
    assert check_repair_invariants(FILES, after) == []


@pytest.mark.parametrize("mutation,expected", [
    (lambda s: s.replace(SUBNET, ""), "removed aws_subnet.a"),
    (lambda s: s.replace('  enable_dns_support = true\n',
                         '  enable_dns_support = true\n  lifecycle {\n    ignore_changes = all\n  }\n'), "ignore_changes"),
    (lambda s: s.replace('resource "aws_vpc"', '#checkov:skip=CKV2_AWS_11\nresource "aws_vpc"'), "suppression"),
    (lambda s: s + '\ndata "external" "x" {\n  program = ["sh"]\n}\n', "external data source"),
    (lambda s: s.replace('  enable_dns_support = true\n',
                         '  enable_dns_support = true\n  provisioner "local-exec" {\n    command = "echo"\n  }\n'), "provisioner"),
    (lambda s: s.replace('"10.0.0.0/16"', '"10.0.0.0/16"\n  password = "hunter22"'), "literal secret"),
    (lambda s: s.replace('"10.0.0.0/16"', '"10.0.0.0/16"\n  key = "AKIAABCDEFGHIJKLMNOP"'), "AWS access key"),
])
def test_invariant_violations_are_caught(mutation, expected):
    after = {"foundation.tf": mutation(FILES["foundation.tf"])}
    violations = check_repair_invariants(FILES, after)
    assert any(expected in v for v in violations), violations


def test_pre_existing_patterns_do_not_count_against_a_repair():
    before = {"a.tf": VPC.replace("}\n", "  lifecycle {\n    ignore_changes = [tags]\n  }\n}\n")}
    after = {"a.tf": before["a.tf"].replace("enable_dns_support", "enable_dns_hostnames")}
    assert check_repair_invariants(before, after) == []


# --- diagnostics ------------------------------------------------------------

def test_enclosing_block_maps_lines_to_addresses():
    content = FILES["foundation.tf"]
    assert enclosing_block(content, 3) == "aws_vpc.main"
    assert enclosing_block(content, 5) is None  # blank line between blocks
    assert enclosing_block(content, 7) == "aws_subnet.a"


def test_parse_json_validate_diagnostics():
    output = json.dumps({"valid": False, "diagnostics": [
        {"severity": "error", "summary": "Unsupported argument", "detail": "An argument named \"x\" is not expected.",
         "range": {"filename": "foundation.tf", "start": {"line": 3}}},
        {"severity": "warning", "summary": "Deprecated", "range": {"filename": "foundation.tf", "start": {"line": 2}}},
    ]})
    diags = parse_diagnostics({"checks": [{"check_name": "validate", "passed": False, "output": output}]}, FILES)
    assert len(diags) == 1
    assert diags[0]["address"] == "aws_vpc.main" and diags[0]["summary"] == "Unsupported argument"


def test_parse_text_init_errors():
    output = "\x1b[31mError: Invalid block definition\x1b[0m\n\n  on foundation.tf line 7, in resource \"aws_subnet\" \"a\":\n"
    diags = parse_diagnostics({"checks": [{"check_name": "init", "passed": False, "output": output}]}, FILES)
    assert diags[0]["address"] == "aws_subnet.a"


# --- validation repair ------------------------------------------------------

import agents.validation_repair as vr  # noqa: E402

BAD_VALIDATE = {"checks": [{"check_name": "validate", "passed": False, "output": json.dumps({"diagnostics": [
    {"severity": "error", "summary": "Unsupported argument", "detail": "enable_dns_supportt",
     "range": {"filename": "foundation.tf", "start": {"line": 3}}}]})}]}


@pytest.fixture
def quiet(monkeypatch):
    async def publish(*args, **kwargs):
        return None
    monkeypatch.setattr(vr.redis_service, "publish_log", publish)


def _state(files: Dict[str, str]) -> Dict[str, Any]:
    return {"job_id": "job-r", "terraform_files": files, "validation_results": BAD_VALIDATE, "repair_attempts": 0}


async def test_repair_applies_a_clean_fix(monkeypatch, quiet):
    broken = {"foundation.tf": FILES["foundation.tf"].replace("enable_dns_support", "enable_dns_supportt")}

    async def llm(block, error):
        return block.replace("enable_dns_supportt", "enable_dns_support")
    monkeypatch.setattr(vr, "_llm_fix", llm)

    out = await vr.repair_validation_node(_state(broken))

    assert out["terraform_files"] == FILES
    assert out["repair_history"][0]["fixed"] == ["aws_vpc.main"]
    assert out["repair_attempts"] == 1


async def test_repair_rejects_a_fix_that_breaks_invariants(monkeypatch, quiet):
    broken = {"foundation.tf": FILES["foundation.tf"].replace("enable_dns_support", "enable_dns_supportt")}

    async def llm(block, error):  # "fixes" the error by silencing it
        return block.replace("  enable_dns_supportt = true\n", "  lifecycle {\n    ignore_changes = all\n  }\n")
    monkeypatch.setattr(vr, "_llm_fix", llm)

    out = await vr.repair_validation_node(_state(broken))

    assert out["terraform_files"] == broken  # left exactly as it was
    assert out["repair_history"][0]["rejected"][0]["address"] == "aws_vpc.main"
    assert out["repair_history"][0]["fixed"] == []


async def test_repair_records_unresolved_when_llm_unavailable(monkeypatch, quiet):
    async def llm(block, error):
        return None
    monkeypatch.setattr(vr, "_llm_fix", llm)

    out = await vr.repair_validation_node(_state(FILES))

    assert out["repair_history"][0]["unresolved"]


# --- fail-closed scanners ---------------------------------------------------

from tools.checkov_runner import CheckovRunner  # noqa: E402
from tools.conftest_runner import ConftestRunner  # noqa: E402
from tools.trivy_runner import TrivyRunner  # noqa: E402


class _Proc:
    def __init__(self, stdout: bytes, returncode: int, stderr: bytes = b""):
        self._out, self.returncode, self._err = stdout, returncode, stderr

    async def communicate(self):
        return self._out, self._err

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


def _fake_exec(monkeypatch, module, proc):
    async def exec_(*args, **kwargs):
        return proc
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", exec_)


@pytest.mark.parametrize("runner", [CheckovRunner, TrivyRunner, ConftestRunner])
async def test_unparseable_output_is_a_failure_not_clean(monkeypatch, runner):
    _fake_exec(monkeypatch, sys.modules[runner.__module__], _Proc(b"not json {", 0))
    res = await runner.scan_hcl({"a.tf": VPC})
    assert res["tool_error"] == "unparseable JSON output" and res["findings"] == []


@pytest.mark.parametrize("runner", [CheckovRunner, TrivyRunner, ConftestRunner])
async def test_crash_with_no_output_is_a_failure(monkeypatch, runner):
    _fake_exec(monkeypatch, sys.modules[runner.__module__], _Proc(b"", 2, b"segfault"))
    res = await runner.scan_hcl({"a.tf": VPC})
    assert res["tool_error"] and "exited 2" in res["tool_error"]


@pytest.mark.parametrize("runner", [CheckovRunner, TrivyRunner, ConftestRunner])
async def test_timeout_is_a_failure(monkeypatch, runner):
    import asyncio

    class _Hang(_Proc):
        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

    module = sys.modules[runner.__module__]
    monkeypatch.setattr(module, "SCANNER_TIMEOUT_SECONDS", 0.05)
    _fake_exec(monkeypatch, module, _Hang(b"", 0))
    res = await runner.scan_hcl({"a.tf": VPC})
    assert res["tool_error"] and "timed out" in res["tool_error"]


async def test_policy_step_reports_failed_scanners_and_drops_tfsec(monkeypatch):
    import agents.policy_agent as pa

    async def ok(files):
        return {"findings": [], "tool_skipped": False, "tool_error": None}

    async def broken(files):
        return {"findings": [], "tool_skipped": False, "tool_error": "unparseable JSON output"}

    async def publish(*a, **k):
        return None

    monkeypatch.setattr(pa.CheckovRunner, "scan_hcl", staticmethod(ok))
    monkeypatch.setattr(pa.TrivyRunner, "scan_hcl", staticmethod(broken))
    monkeypatch.setattr(pa.ConftestRunner, "scan_hcl", staticmethod(ok))
    monkeypatch.setattr(pa.redis_service, "publish_log", publish)

    out = await pa.policy_agent_node({"job_id": "j", "terraform_files": {"a.tf": VPC}})

    assert out["security_results"]["scanners_failed"] == {"trivy": "unparseable JSON output"}
    assert not hasattr(pa, "TfsecRunner")
