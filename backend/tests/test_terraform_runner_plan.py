"""Unit tests for TerraformRunner.plan_json - the real `init && plan && show
-json` workflow behind the Plan Equivalence Agent. Mocks the subprocess layer
(same pattern as test_infracost_runner.py) so these run fast and without a
real Terraform binary or AWS account; a live end-to-end run happens through
plan_equivalence_agent_node against a genuine provider handshake instead.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from tools.terraform_runner import TerraformRunner

SAMPLE_PLAN_JSON = json.dumps({
    "resource_changes": [
        {"address": "aws_vpc.main", "change": {"actions": ["no-op"]}},
        {"address": "aws_subnet.new", "change": {"actions": ["create"]}},
        {"address": "aws_s3_bucket.tags_only", "change": {"actions": ["update"]}},
        {"address": "aws_db_instance.mydb", "change": {"actions": ["delete", "create"]}},
        {"address": "aws_iam_role.orphan", "change": {"actions": ["delete"]}},
    ]
})


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


def _creds():
    return {"access_key": "AKIAFAKEFAKEFAKEFAKE", "secret_key": "fakefakefakefakefakefakefakefakefakefake", "session_token": None}


@pytest.mark.asyncio
async def test_plan_json_tallies_create_update_replace_destroy(monkeypatch):
    processes = [
        _FakeProcess(stdout=b"init ok"),
        _FakeProcess(stdout=b"plan ok"),
        _FakeProcess(stdout=SAMPLE_PLAN_JSON.encode("utf-8")),
    ]
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=processes)):
        result = await TerraformRunner.plan_json(
            {"main.tf": 'resource "aws_vpc" "main" {}'}, aws_credentials=_creds(), region="us-east-1"
        )

    assert result["no_op"] == 1
    assert result["create"] == 1
    assert result["update"] == 1
    assert result["replace"] == 1
    assert result["destroy"] == 1
    assert result["passed"] is False
    assert {"address": "aws_db_instance.mydb", "action": "replace"} in result["blocking_actions"]
    assert {"address": "aws_iam_role.orphan", "action": "destroy"} in result["blocking_actions"]


@pytest.mark.asyncio
async def test_plan_json_passes_when_only_create_and_no_op(monkeypatch):
    clean_plan = json.dumps({
        "resource_changes": [
            {"address": "aws_vpc.main", "change": {"actions": ["no-op"]}},
            {"address": "aws_subnet.new", "change": {"actions": ["create"]}},
        ]
    })
    processes = [
        _FakeProcess(stdout=b"init ok"),
        _FakeProcess(stdout=b"plan ok"),
        _FakeProcess(stdout=clean_plan.encode("utf-8")),
    ]
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=processes)):
        result = await TerraformRunner.plan_json(
            {"main.tf": 'resource "aws_vpc" "main" {}'}, aws_credentials=_creds(), region="us-east-1"
        )

    assert result["passed"] is True
    assert result["blocking_actions"] == []


@pytest.mark.asyncio
async def test_plan_json_stops_and_reports_when_init_fails(monkeypatch):
    processes = [_FakeProcess(stdout=b"", stderr=b"no provider found", returncode=1)]
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=processes)):
        result = await TerraformRunner.plan_json(
            {"main.tf": 'resource "aws_vpc" "main" {}'}, aws_credentials=_creds(), region="us-east-1"
        )

    assert result["passed"] is False
    assert result["checks"][0]["check_name"] == "init"
    assert result["checks"][0]["passed"] is False
    # Only init should have run - plan/show never got a chance to.
    assert len(result["checks"]) == 1


@pytest.mark.asyncio
async def test_plan_json_scrubs_credentials_from_output(monkeypatch):
    leaky_output = "AWS_SECRET_ACCESS_KEY=fakefakefakefakefakefakefakefakefakefake failed to authenticate"
    processes = [_FakeProcess(stdout=b"", stderr=leaky_output.encode("utf-8"), returncode=1)]
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=processes)):
        result = await TerraformRunner.plan_json(
            {"main.tf": 'resource "aws_vpc" "main" {}'}, aws_credentials=_creds(), region="us-east-1"
        )

    assert "fakefakefakefakefakefakefakefakefakefake" not in result["checks"][0]["output"]


@pytest.mark.asyncio
async def test_plan_json_never_inherits_parent_process_env(monkeypatch):
    """The scoped env dict passed to the subprocess must never be the parent's
    entire os.environ - only PATH/HOME/TF_PLUGIN_CACHE_DIR plus the request's
    own AWS credentials. This is the actual safety property the feature
    description asks for ("keep credentials tightly scoped")."""
    captured_envs = []

    async def fake_exec(*args, **kwargs):
        captured_envs.append(kwargs.get("env"))
        return _FakeProcess(stdout=b"init ok")

    monkeypatch.setenv("SOME_UNRELATED_SECRET", "should-never-appear")
    monkeypatch.setenv("TF_LOG", "TRACE")
    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        await TerraformRunner.plan_json(
            {"main.tf": 'resource "aws_vpc" "main" {}'}, aws_credentials=_creds(), region="us-east-1"
        )

    assert captured_envs, "run_command should have invoked the subprocess"
    env = captured_envs[0]
    assert "SOME_UNRELATED_SECRET" not in env
    assert "TF_LOG" not in env
    assert "TF_LOG_PATH" not in env
    assert env["AWS_ACCESS_KEY_ID"] == _creds()["access_key"]
