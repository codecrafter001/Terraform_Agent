"""A deployment plan once failed as "Terraform plan failed: " and nothing else: uvicorn
--reload on Windows runs a SelectorEventLoop, asyncio.create_subprocess_exec raised
NotImplementedError() (str() == ""), and the plan role's credentials were dropped because
plan_session returns env-var names. These pin each piece of the fix."""

import sys
from unittest.mock import AsyncMock, patch

import pytest

from deploy.pipeline import plan_failure_message
from deploy.sts import plan_session
from tools.subprocess_exec import describe_exception, run_exec
from tools.terraform_runner import TerraformRunner


async def test_run_exec_falls_back_when_the_loop_cannot_spawn_subprocesses():
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=NotImplementedError())):
        code, out, err = await run_exec(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"]
        )
    assert code == 3
    assert out.strip() == b"out"
    assert err.strip() == b"err"


async def test_run_exec_fallback_enforces_the_timeout():
    import asyncio

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=NotImplementedError())):
        with pytest.raises(asyncio.TimeoutError):
            await run_exec([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)


@pytest.mark.parametrize("cmd", [
    ["terraform", "apply", "tfplan"],
    ["tofu.exe", "plan", "-destroy"],
    ["terraform", "import", "aws_vpc.a", "vpc-1"],
])
async def test_run_exec_refuses_mutating_terraform(cmd):
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn:
        with pytest.raises(ValueError, match="Safety Violation"):
            await run_exec(cmd)
    spawn.assert_not_called()


def test_describe_exception_is_never_blank():
    assert describe_exception(NotImplementedError()) == "NotImplementedError (no message)"
    assert describe_exception(RuntimeError("boom")) == "RuntimeError: boom"


async def test_plan_saved_reports_a_spawn_failure_with_text():
    creds = {"access_key": "AKIAFAKEFAKEFAKEFAKE", "secret_key": "x" * 40}
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=PermissionError())):
        res = await TerraformRunner.plan_saved(workdir=".", target={"id": "t"}, deployment_id="dep-1",
                                               aws_credentials=creds)
    assert res["passed"] is False
    assert res["checks"][-1]["output"] == "PermissionError (no message)"
    assert plan_failure_message(res).startswith("Terraform plan failed at 'system':\nPermissionError")


def test_plan_role_credentials_reach_terraform(monkeypatch):
    monkeypatch.setattr("deploy.sts.assume_role", lambda **kw: ("ASIAPLANROLE", "plan-secret", "plan-token"))
    creds = plan_session({"id": "t", "state_bucket": "b", "plan_role_arn": "arn:aws:iam::1:role/p"}, "dep-1")
    env = TerraformRunner._scoped_aws_env(creds, "us-east-1")
    assert env["AWS_ACCESS_KEY_ID"] == "ASIAPLANROLE"
    assert env["AWS_SECRET_ACCESS_KEY"] == "plan-secret"
    assert env["AWS_SESSION_TOKEN"] == "plan-token"


async def test_plan_saved_refuses_to_run_without_credentials():
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn:
        res = await TerraformRunner.plan_saved(workdir=".", target={"id": "t"}, deployment_id="dep-1",
                                               aws_credentials={})
    spawn.assert_not_called()
    assert res["passed"] is False and res["checks"][0]["check_name"] == "credentials"


def test_plan_failure_message_shows_terraform_error_and_step():
    res = {"checks": [
        {"check_name": "init", "passed": False,
         "output": "Initializing the backend...\nInitializing provider plugins...\n"
                   "Error: Failed to install provider\n\nError while installing hashicorp/aws"},
    ]}
    assert plan_failure_message(res) == (
        "Terraform plan failed at 'init':\nError: Failed to install provider\n\n"
        "Error while installing hashicorp/aws"
    )


def test_plan_failure_message_with_empty_output_still_says_something():
    msg = plan_failure_message({"checks": [{"check_name": "plan", "passed": False, "output": ""}]})
    assert msg == "Terraform plan failed at 'plan' with no output on stdout or stderr."
    assert plan_failure_message({"checks": []}).startswith("Terraform plan failed: no step")


# --- Host (non-Docker) runs: scanners and network failures -----------------------

def test_security_policies_resolve_on_a_host_run():
    import os

    from tools.security_paths import security_path

    assert os.path.isdir(security_path("policies"))
    assert os.path.isfile(security_path("checkov", ".checkov.yaml"))


@pytest.mark.skipif(sys.platform != "win32", reason="PATHEXT lookup is Windows-only")
def test_cmd_shims_are_found_on_windows(tmp_path):
    from tools.subprocess_exec import _resolve_binary

    shim = tmp_path / "fakescanner.cmd"
    shim.write_text("@echo off\r\n")
    resolved = _resolve_binary(["fakescanner", "-v"], {"PATH": str(tmp_path)})
    assert resolved[0].lower() == str(shim).lower() and resolved[1:] == ["-v"]


async def test_validate_init_network_failure_is_incomplete_not_a_template_bug(monkeypatch):
    from deploy import verify as verify_mod

    async def fake_run_command(cmd, cwd, env=None, timeout_seconds=None):
        if "init" in cmd:
            return 1, "", ("Error: Failed to query available provider packages\n"
                           "dial tcp: lookup registry.terraform.io: no such host")
        return 0, "", ""

    monkeypatch.setattr(TerraformRunner, "run_command", staticmethod(fake_run_command))
    result = await TerraformRunner.validate_hcl({"main.tf": "terraform {}\n"})
    names = [c["check_name"] for c in result["checks"]]
    assert result["passed"] is False and "system" in names

    async def no_policy(state):
        return {"security_results": {"findings": [], "scanners_skipped": [], "scanners_failed": {}}}

    async def no_cost(files):
        return {}

    monkeypatch.setattr(verify_mod, "policy_agent_node", no_policy)
    monkeypatch.setattr(verify_mod.InfracostRunner, "estimate_cost", staticmethod(no_cost))
    v = await verify_mod.verify("dep-1", {"main.tf": "terraform {}\n"})
    assert v["verdict"] == "INCOMPLETE"

