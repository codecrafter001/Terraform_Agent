"""Unit tests for InfracostRunner - a missing binary, missing API key, or
malformed output must always report tool_skipped=True, never a fabricated
$0.00 that looks like a genuine clean estimate."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from tools.infracost_runner import InfracostRunner

SAMPLE_BREAKDOWN_JSON = json.dumps({
    "currency": "USD",
    "totalMonthlyCost": "42.50",
    "projects": [
        {
            "breakdown": {
                "resources": [
                    {"name": "aws_instance.web", "resourceType": "aws_instance", "monthlyCost": "30.00"},
                    {"name": "aws_s3_bucket.data", "resourceType": "aws_s3_bucket", "monthlyCost": "12.50"},
                ]
            }
        }
    ],
    "summary": {"totalUnsupportedResources": 1},
})


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_missing_api_key_reports_tool_skipped_not_zero_cost(monkeypatch):
    monkeypatch.delenv("INFRACOST_API_KEY", raising=False)
    result = await InfracostRunner.estimate_cost({"main.tf": "resource \"aws_vpc\" \"x\" {}"})

    assert result["tool_skipped"] is True
    assert result["total_monthly_cost"] == 0.0


@pytest.mark.asyncio
async def test_missing_binary_reports_tool_skipped(monkeypatch):
    monkeypatch.setenv("INFRACOST_API_KEY", "fake-key")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=FileNotFoundError())):
        result = await InfracostRunner.estimate_cost({"main.tf": "resource \"aws_vpc\" \"x\" {}"})

    assert result["tool_skipped"] is True
    assert result["total_monthly_cost"] == 0.0


@pytest.mark.asyncio
async def test_malformed_json_output_reports_tool_skipped_no_crash(monkeypatch):
    monkeypatch.setenv("INFRACOST_API_KEY", "fake-key")
    fake_process = _FakeProcess(stdout=b"not valid json{{{")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_process)):
        result = await InfracostRunner.estimate_cost({"main.tf": "resource \"aws_vpc\" \"x\" {}"})

    assert result["tool_skipped"] is True


@pytest.mark.asyncio
async def test_nonzero_exit_code_reports_tool_skipped(monkeypatch):
    monkeypatch.setenv("INFRACOST_API_KEY", "fake-key")
    fake_process = _FakeProcess(stdout=b"", stderr=b"invalid api key", returncode=1)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_process)):
        result = await InfracostRunner.estimate_cost({"main.tf": "resource \"aws_vpc\" \"x\" {}"})

    assert result["tool_skipped"] is True


@pytest.mark.asyncio
async def test_real_breakdown_json_parses_correctly(monkeypatch):
    monkeypatch.setenv("INFRACOST_API_KEY", "fake-key")
    fake_process = _FakeProcess(stdout=SAMPLE_BREAKDOWN_JSON.encode("utf-8"))
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_process)):
        result = await InfracostRunner.estimate_cost({"main.tf": "resource \"aws_instance\" \"web\" {}"})

    assert result["tool_skipped"] is False
    assert result["total_monthly_cost"] == 42.50
    assert result["currency"] == "USD"
    assert result["unsupported_resource_count"] == 1
    # sorted descending by cost
    assert [r["name"] for r in result["resources"]] == ["aws_instance.web", "aws_s3_bucket.data"]
    assert result["resources"][0]["monthly_cost"] == 30.00
