"""One way to run a CLI tool and capture its output from async code, on any event loop.

asyncio.create_subprocess_exec raises NotImplementedError - with an empty message - on a
loop without subprocess support. That is what uvicorn runs on Windows with --reload or
--workers (it builds a SelectorEventLoop directly, so set_event_loop_policy has no
effect), and it surfaced as "Terraform plan failed: " with nothing after it. On such a
loop the command runs through subprocess.run in a worker thread instead: same argv, cwd,
env and timeout, same (returncode, stdout, stderr) result.

terraform/tofu argv is checked with check_argv here too, so this helper can never run
apply/destroy/import - TerraformRunner.run_command remains the way to call terraform.
"""

import asyncio
import os
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple


async def run_exec(
    cmd: List[str],
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = None,
    merge_stderr: bool = False,
) -> Tuple[int, bytes, bytes]:
    """Run cmd and return (returncode, stdout, stderr). stdin is /dev/null. Raises
    asyncio.TimeoutError (the process is killed first) after `timeout` seconds and
    FileNotFoundError if the binary isn't installed. merge_stderr sends stderr into
    stdout (stderr is then b"")."""
    binary = os.path.basename(cmd[0]).lower() if cmd else ""
    if binary.removesuffix(".exe") in ("terraform", "tofu"):
        from tools.terraform_runner import check_argv

        check_argv(cmd)
    cmd = _resolve_binary(cmd, env)

    stderr_target = asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_target,
        )
    except NotImplementedError:
        return await asyncio.to_thread(_run_blocking, cmd, cwd, env, timeout, merge_stderr)

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, stdout or b"", stderr or b""


def _resolve_binary(cmd: List[str], env: Optional[Dict[str, str]]) -> List[str]:
    """Windows only finds a bare name if it's an .exe, and pip installs checkov as
    checkov.cmd - so it looked "not installed". Resolve through PATH/PATHEXT first;
    an unknown name is left alone and still raises FileNotFoundError."""
    if sys.platform != "win32" or not cmd or os.path.dirname(cmd[0]):
        return cmd
    path = (env or {}).get("PATH") if env is not None else None
    found = shutil.which(cmd[0], path=path)
    return [found, *cmd[1:]] if found else cmd


def describe_exception(e: BaseException) -> str:
    """Never an empty string: str(NotImplementedError()) is "", which is how a failed
    plan once reached the UI as "Terraform plan failed: " and nothing else."""
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else f"{type(e).__name__} (no message)"


def _run_blocking(
    cmd: List[str],
    cwd: Optional[str],
    env: Optional[Dict[str, str]],
    timeout: Optional[float],
    merge_stderr: bool,
) -> Tuple[int, bytes, bytes]:
    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise asyncio.TimeoutError(f"'{' '.join(cmd[:2])}' timed out after {timeout}s") from e
    return completed.returncode, completed.stdout or b"", completed.stderr or b""
