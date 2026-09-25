"""AWS Resource Explorer inventory (tools/resource_explorer.py) and the
discovery step that uses it (agents/resource_explorer_step.py)."""

from typing import Any, Dict, List

import pytest
from botocore.exceptions import ClientError

import tools.resource_explorer as rex
from tools.resource_explorer import READ_ONLY_OPERATIONS, ResourceExplorerInventory


def _res(rtype: str, region: str, name: str = "") -> Dict[str, Any]:
    props = [{"Name": "tags", "Data": [{"Key": "Name", "Value": name}]}] if name else []
    return {"Arn": f"arn:aws:{rtype.split(':')[0]}:{region}:123:{rtype.split(':')[1]}/x", "ResourceType": rtype,
            "Region": region, "Properties": props}


class FakeClient:
    """Records every operation; raises if anything outside READ_ONLY_OPERATIONS is touched."""

    def __init__(self, calls: List[str], indexes, pages, error=None):
        self._calls, self._indexes, self._pages, self._error = calls, indexes, list(pages), error

    def __getattr__(self, op):
        raise AssertionError(f"non-allowlisted Resource Explorer call: {op}")

    def list_indexes(self, **kwargs):
        self._calls.append("list_indexes")
        if self._error:
            raise self._error
        return {"Indexes": self._indexes}

    def search(self, **kwargs):
        self._calls.append("search")
        return self._pages.pop(0)


def _explorer(monkeypatch, indexes, pages, error=None):
    calls: List[str] = []
    ex = ResourceExplorerInventory("AKIAFAKE", "secret", "us-east-1")
    monkeypatch.setattr(ex, "_client", lambda region: FakeClient(calls, indexes, pages, error))
    return ex, calls


AGG = [{"Arn": "arn:idx", "Region": "eu-west-1", "Type": "AGGREGATOR"}, {"Region": "us-east-1", "Type": "LOCAL"}]


def test_inventory_aggregates_and_suggests_region_with_most_supported(monkeypatch):
    rows = (
        [_res("ec2:instance", "ap-south-1", "web")] * 3
        + [_res("ec2:vpc", "ap-south-1")]
        + [_res("s3:bucket", "us-east-1")]
        + [_res("iam:role", "global")] * 5  # global - must never pick the region
        + [_res("lambda:function", "us-east-1")] * 4  # not a supported type
    )
    ex, calls = _explorer(monkeypatch, AGG, [{"Resources": rows, "Count": {"TotalResources": len(rows), "Complete": True}}])

    inv = ex.collect(["EC2", "VPC", "S3", "IAM", "SG", "RDS"])

    assert set(calls) <= set(READ_ONLY_OPERATIONS)
    assert inv["available"] and inv["aggregated"] and inv["index_region"] == "eu-west-1"
    assert inv["total"] == 14 and inv["supported_total"] == 10 and inv["unsupported_total"] == 4
    assert inv["suggested_region"] == "ap-south-1"
    assert inv["supported_by_region"] == {"ap-south-1": 4, "us-east-1": 1}
    assert inv["resources"][0]["name"] == "web"


def test_filters_limit_what_counts_as_supported(monkeypatch):
    rows = [_res("ec2:instance", "ap-south-1")] * 3 + [_res("s3:bucket", "us-east-1")]
    ex, _ = _explorer(monkeypatch, AGG, [{"Resources": rows, "Count": {"TotalResources": 4, "Complete": True}}])

    inv = ex.collect(["S3"])

    assert inv["supported_total"] == 1 and inv["suggested_region"] == "us-east-1"


def test_paginates_and_flags_truncation(monkeypatch):
    monkeypatch.setattr(rex, "MAX_RESOURCES", 3)
    page1 = {"Resources": [_res("ec2:vpc", "us-east-1")] * 2, "NextToken": "t", "Count": {"TotalResources": 3, "Complete": False}}
    page2 = {"Resources": [_res("ec2:vpc", "us-east-1")]}
    ex, calls = _explorer(monkeypatch, AGG, [page1, page2])

    inv = ex.collect(["VPC"])

    assert calls.count("search") == 2
    assert inv["returned"] == 3 and inv["truncated"]


@pytest.mark.parametrize(
    "indexes,error,expect",
    [
        ([], None, "not turned on"),
        (AGG, ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "ListIndexes"), "permissions"),
        (AGG, ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "no"}}, "ListIndexes"), "default view"),
    ],
)
def test_reports_unavailable_instead_of_failing(monkeypatch, indexes, error, expect):
    ex, calls = _explorer(monkeypatch, indexes, [], error)

    inv = ex.collect()

    assert inv["available"] is False and expect in inv["reason"]
    assert "search" not in calls or error is None


def test_local_index_only_is_marked_not_aggregated(monkeypatch):
    ex, _ = _explorer(monkeypatch, [{"Region": "us-east-1", "Type": "LOCAL"}],
                      [{"Resources": [_res("ec2:vpc", "us-east-1")], "Count": {"TotalResources": 1, "Complete": True}}])

    inv = ex.collect(["VPC"])

    assert inv["available"] and inv["aggregated"] is False


def test_only_read_only_operations_are_allowlisted():
    mutating_prefixes = ("create", "delete", "update", "associate", "disassociate", "tag", "untag", "put")
    assert not any(op.startswith(mutating_prefixes) for op in READ_ONLY_OPERATIONS)


# --- discovery step -------------------------------------------------------

import agents.resource_explorer_step as step  # noqa: E402


@pytest.fixture
def quiet_logs(monkeypatch):
    logged: List[str] = []

    async def capture(job_id, message):
        logged.append(message)

    monkeypatch.setattr(step, "_log", capture)
    return logged


def _state(region: str, **extra) -> Dict[str, Any]:
    return {"job_id": "job-rex", "region": region, "resource_filters": ["EC2", "VPC"],
            "aws_credentials": {"access_key": "AKIAFAKE", "secret_key": "s"}, **extra}


def _fake_inventory(monkeypatch, inventory):
    class Fake:
        def __init__(self, *a, **k):
            pass

        def collect(self, filters):
            return inventory

    monkeypatch.setattr(step, "ResourceExplorerInventory", Fake)


INV = {"available": True, "aggregated": True, "index_region": "us-east-1", "total": 9, "truncated": False,
       "supported_total": 6, "by_region": {"ap-south-1": 6, "us-east-1": 3},
       "supported_by_region": {"ap-south-1": 6}, "suggested_region": "ap-south-1"}


async def test_auto_region_resolves_to_suggested(monkeypatch, quiet_logs):
    _fake_inventory(monkeypatch, INV)

    out = await step.resource_explorer_node(_state("auto"))

    assert out["region"] == "ap-south-1" and out["requested_region"] == "auto"
    assert out["resource_inventory"]["available"]


async def test_auto_region_falls_back_when_unavailable(monkeypatch, quiet_logs):
    _fake_inventory(monkeypatch, {"available": False, "reason": "not turned on"})

    out = await step.resource_explorer_node(_state("auto"))

    assert out["region"] == "us-east-1"
    assert any("falling back" in m for m in quiet_logs)


async def test_explicit_empty_region_warns_but_is_not_overridden(monkeypatch, quiet_logs):
    _fake_inventory(monkeypatch, INV)

    out = await step.resource_explorer_node(_state("us-east-1"))

    assert "region" not in out  # the user's explicit choice stands
    assert any("Warning: us-east-1 has no supported resources" in m for m in quiet_logs)


async def test_skipped_when_turned_off(monkeypatch, quiet_logs):
    _fake_inventory(monkeypatch, INV)

    out = await step.resource_explorer_node(_state("us-east-1", use_resource_explorer=False))

    assert out["resource_inventory"]["available"] is False
