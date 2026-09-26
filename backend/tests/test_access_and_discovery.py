"""Week 7 access hardening: ExternalId on AssumeRole, every live check reads
the target account, incomplete discovery fails closed, and the runner only
runs allowlisted terraform subcommands."""

import pytest

import agents.cloud_discovery as discovery_mod
import agents.graph as graph_mod
from tools.terraform_runner import TerraformRunner


@pytest.fixture
def quiet(monkeypatch):
    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(discovery_mod.redis_service, "publish_log", nothing)


class FakeScanner:
    def __init__(self, **kwargs):
        FakeScanner.kwargs = kwargs

    def scan_all(self, filters):
        return [{"id": "vpc-1", "resource_type": "aws_vpc"}]

    def report(self):
        return {"region": "us-east-1", "complete": False, "counts": {"vpcs": 1},
                "errors": [{"scope": "EC2 instances", "code": "Throttling", "message": "slow down"}]}


async def test_role_is_assumed_with_external_id_and_its_credentials_replace_the_callers(monkeypatch, quiet):
    seen = {}

    def fake_assume(role_arn, ak, sk, token, region, session_name, endpoint_url=None, external_id=None):
        seen.update(role_arn=role_arn, external_id=external_id)
        return "ASIATEMP", "temp-secret", "temp-token"

    monkeypatch.setattr(discovery_mod, "assume_role", fake_assume)
    monkeypatch.setattr(discovery_mod, "AWSScanner", FakeScanner)
    out = await discovery_mod.cloud_discovery_node({
        "job_id": "j", "region": "us-east-1", "role_arn": "arn:aws:iam::111122223333:role/terraagent-read",
        "external_id": "tenant-42", "aws_credentials": {"access_key": "AKIALONGLIVED", "secret_key": "s"},
    })
    assert seen == {"role_arn": "arn:aws:iam::111122223333:role/terraagent-read", "external_id": "tenant-42"}
    assert FakeScanner.kwargs["access_key"] == "ASIATEMP"
    # drift / plan / cross-check read state["aws_credentials"]: now the target account's
    assert out["aws_credentials"] == {"access_key": "ASIATEMP", "secret_key": "temp-secret",
                                      "session_token": "temp-token"}
    assert out["discovery"]["complete"] is False


async def test_failed_assume_role_is_an_incomplete_scan_not_an_empty_account(monkeypatch, quiet):
    from botocore.exceptions import ClientError

    def fail(*a, **k):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "AssumeRole")

    monkeypatch.setattr(discovery_mod, "assume_role", fail)
    out = await discovery_mod.cloud_discovery_node({
        "job_id": "j", "role_arn": "arn:aws:iam::1:role/x",
        "aws_credentials": {"access_key": "AKIA", "secret_key": "s"},
    })
    assert out["resources"] == [] and out["discovery"]["complete"] is False
    assert "aws_credentials" not in out


def test_incomplete_discovery_makes_the_verdict_incomplete():
    acc = {"discovery": {"complete": False, "errors": [{"scope": "S3", "code": "Throttling"}]},
           "validation_results": {"passed": True, "checks": []},
           "security_results": {"findings": [], "scanners_skipped": [], "scanners_failed": {}}}
    it = graph_mod._record_iteration(acc, validation_only=False)
    assert it["verdict"] == "INCOMPLETE"
    assert "discovery was incomplete" in it["incomplete_reasons"][0]


@pytest.mark.parametrize("argv", [
    ["terraform", "apply"], ["terraform", "destroy"], ["tofu", "import", "a", "b"],
    ["terraform", "plan", "-destroy"], ["terraform", "-chdir=x", "apply"], ["terraform", "state", "rm", "x"],
    ["terraform", "console"], ["terraform", "workspace", "new", "x"], ["terraform", "APPLY"],
])
async def test_runner_refuses_anything_but_allowlisted_subcommands(argv, tmp_path):
    with pytest.raises(ValueError):
        await TerraformRunner.run_command(argv, cwd=str(tmp_path))


def test_published_read_only_policy_never_allows_a_write():
    import json
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "aws", "read-only-policy.json")
    if not os.path.exists(path):
        pytest.skip("docs/ is not part of the backend image")
    with open(path, encoding="utf-8") as f:
        policy = json.load(f)
    allowed = [a for st in policy["Statement"] if st["Effect"] == "Allow" for a in st["Action"]]
    verbs = [a.split(":", 1)[1] for a in allowed]
    assert all(v.startswith(("Describe", "Get", "List", "Search")) for v in verbs), verbs
    denies = {st["Sid"] for st in policy["Statement"] if st["Effect"] == "Deny"}
    assert {"NeverReadData", "NeverChangeAnything"} <= denies
