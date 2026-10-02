"""Terraform CLI runner for validating HCL code in sandboxed directories.
Enforces safety in run_command: only the terraform/tofu binaries, only the
allowlisted subcommands (version, fmt, init, validate, plan, show, providers),
and apply/destroy/import refused anywhere in argv - including as a flag
(`plan -destroy`).
"""

import asyncio
import contextlib
import json
import logging
import os
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

try:  # POSIX only (the containers); on a Windows dev host init just isn't serialized
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from tools.credential_scrubber import CredentialScrubber
from tools.sandbox_registry import create_sandbox, release_sandbox

logger = logging.getLogger("terraagent.terraform_runner")

# Upper bound on any single terraform/tofu invocation. Generous by default: a
# first `init` may legitimately download a large provider.
TERRAFORM_COMMAND_TIMEOUT_SECONDS = float(os.getenv("TERRAAGENT_TF_COMMAND_TIMEOUT", "600"))

ALLOWED_BINARIES = frozenset({"terraform", "tofu"})
ALLOWED_SUBCOMMANDS = frozenset({"version", "fmt", "init", "validate", "plan", "show", "providers"})
BLOCKED_WORDS = frozenset({"apply", "destroy", "import"})


def check_argv(cmd: List[str]) -> str:
    """Raise ValueError("Safety Violation: ...") unless cmd is an allowlisted
    terraform/tofu subcommand with no apply/destroy/import anywhere. Returns
    the subcommand."""
    if not cmd:
        raise ValueError("Safety Violation: empty command.")
    for arg in cmd:
        token = arg.lower().lstrip("-").split("=", 1)[0]
        if token in BLOCKED_WORDS:
            raise ValueError(f"Safety Violation: '{arg}' is strictly prohibited.")
    binary = os.path.basename(cmd[0]).lower()
    if binary.endswith(".exe"):
        binary = binary[:-4]
    if binary not in ALLOWED_BINARIES:
        raise ValueError(f"Safety Violation: only terraform/tofu may run here, not '{cmd[0]}'.")
    subcommand = next((a for a in cmd[1:] if not a.startswith("-")), None)
    if subcommand not in ALLOWED_SUBCOMMANDS:
        raise ValueError(f"Safety Violation: '{subcommand}' is not an allowed subcommand.")
    return subcommand


@contextlib.asynccontextmanager
async def _plugin_cache_lock(env: Optional[Dict[str, str]]) -> AsyncIterator[None]:
    """Terraform documents the plugin cache as unsafe for concurrent `init`s.
    Several scans (Celery worker processes) share it, so inits take an
    exclusive file lock on the cache - across processes, not just threads.
    Everything else runs concurrently."""
    cache = (env if env is not None else os.environ).get("TF_PLUGIN_CACHE_DIR")
    if not cache or fcntl is None:
        yield
        return
    os.makedirs(cache, exist_ok=True)
    fd = os.open(os.path.join(cache, ".terraagent-init.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _cli_config_path(cache_dir: str) -> str:
    """A Terraform/OpenTofu CLI config that lets `init` reuse the shared
    provider cache. Since Terraform 1.4 the cache is skipped unless the
    working directory already has a lock file entry for the provider, and our
    sandboxes never do - so every `init` downloaded the whole AWS provider
    again (several minutes per validation pass). The sandbox lock file is
    thrown away and never shipped, so the setting's caveat (a lock file with
    only this platform's checksum) doesn't apply. Written once per cache dir,
    next to it; contains no secrets."""
    path = os.path.join(os.path.dirname(cache_dir.rstrip("/")) or cache_dir, "terraagent.tfrc")
    content = (
        f"plugin_cache_dir = {json.dumps(cache_dir)}\n"
        "plugin_cache_may_break_dependency_lock_file = true\n"
    )
    try:
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == content:
                return path
    except OSError:
        pass
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)  # atomic: concurrent workers never see a partial file
    return path


def _with_cache_config(env: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """The subprocess env plus TF_CLI_CONFIG_FILE (cache reuse) and
    CHECKPOINT_DISABLE (skips HashiCorp's version-check call). Leaves env
    untouched when no plugin cache is configured or a CLI config is already
    set. None still means "inherit os.environ", now with these two added."""
    base = env if env is not None else os.environ
    cache = base.get("TF_PLUGIN_CACHE_DIR")
    if not cache or base.get("TF_CLI_CONFIG_FILE"):
        return env
    try:
        config = _cli_config_path(cache)
    except OSError as e:
        logger.warning(f"Could not write the Terraform CLI config, provider cache not reused: {e}")
        return env
    return {**base, "TF_CLI_CONFIG_FILE": config, "CHECKPOINT_DISABLE": "1"}


class TerraformRunner:
    @staticmethod
    async def run_command(
        cmd: List[str], cwd: str, env: Optional[Dict[str, str]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Tuple[int, str, str]:
        """Safely execute a CLI command in the specified working directory.

        env=None (the default, used by every caller except plan_json) means
        "inherit the parent process's environment unchanged" - the same
        behavior this always had. plan_json is the one caller that passes an
        explicit, minimal env dict scoped to a single subprocess call, since
        that's the only command in this file that ever needs real AWS
        credentials."""
        # Hard safety validation - before anything is started.
        subcommand = check_argv(cmd)

        # Without a bound, one stuck command (e.g. `init` downloading a large
        # provider over a slow link) hung the whole job silently. Raising
        # lets callers' existing exception handling report it - validate_hcl
        # turns it into a "system" check, which the graph never sends to repair.
        timeout = timeout_seconds if timeout_seconds is not None else TERRAFORM_COMMAND_TIMEOUT_SECONDS
        env = _with_cache_config(env)
        lock = _plugin_cache_lock(env) if subcommand == "init" else contextlib.nullcontext()
        async with lock:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise TimeoutError(f"'{' '.join(cmd[:2])}' timed out after {int(timeout)}s")
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace")
        )

    @classmethod
    async def format_hcl(cls, hcl_files: Dict[str, str], binary: str = "terraform") -> Dict[str, str]:
        """Canonically reformat HCL content via `<binary> fmt` (pure reformatting -
        never changes semantics, so it cannot introduce new bugs). Falls back to the
        original content for any file where fmt fails to run cleanly.

        binary is "terraform" or "tofu" (models/scan.py::ScanRequest.terraform_binary,
        threaded through TerraAgentState) - OpenTofu is a drop-in-compatible CLI fork
        of Terraform, same subcommands/flags/output shapes, so nothing else here
        needs to branch on which one is running."""
        sandbox_dir = create_sandbox(prefix="terraagent_fmt_")
        try:
            for filename, content in hcl_files.items():
                file_path = os.path.join(sandbox_dir, filename)
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

            code, _, err = await cls.run_command([binary, "fmt"], cwd=sandbox_dir)
            if code != 0:
                logger.warning(f"{binary} fmt failed, keeping unformatted content: {err}")
                return hcl_files

            formatted = {}
            for filename in hcl_files:
                with open(os.path.join(sandbox_dir, filename), "r", encoding="utf-8") as f:
                    formatted[filename] = f.read()
            return formatted
        except Exception as e:
            logger.warning(f"{binary} fmt error, keeping unformatted content: {e}")
            return hcl_files
        finally:
            release_sandbox(sandbox_dir)

    @classmethod
    async def validate_hcl(cls, hcl_files: Dict[str, str], binary: str = "terraform") -> Dict[str, Any]:
        """Write HCL files to sandbox, run init and validate. See format_hcl's
        docstring for what `binary` means."""
        sandbox_dir = create_sandbox(prefix="terraagent_tf_")
        results: Dict[str, Any] = {
            "passed": False,
            "checks": [],
            "sandbox_dir": sandbox_dir
        }

        try:
            # Write HCL files to sandbox
            for filename, content in hcl_files.items():
                file_path = os.path.join(sandbox_dir, filename)
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

            # 1. Format check
            code, out, err = await cls.run_command([binary, "fmt", "-check"], cwd=sandbox_dir)
            results["checks"].append({
                "check_name": "fmt",
                "passed": code == 0,
                "output": out + ("\n" + err if err else "")
            })

            # 2. Init (backend=false)
            code, out, err = await cls.run_command(
                [binary, "init", "-backend=false", "-input=false"],
                cwd=sandbox_dir
            )
            init_passed = code == 0
            results["checks"].append({
                "check_name": "init",
                "passed": init_passed,
                "output": out + ("\n" + err if err else "")
            })

            # 3. Validate
            if init_passed:
                code, out, err = await cls.run_command(
                    [binary, "validate", "-json"],
                    cwd=sandbox_dir
                )
                val_passed = code == 0
                results["checks"].append({
                    "check_name": "validate",
                    "passed": val_passed,
                    "output": out + ("\n" + err if err else "")
                })
                results["passed"] = val_passed
            else:
                results["passed"] = False

        except Exception as e:
            logger.error(f"Validation execution error: {e}")
            results["checks"].append({
                "check_name": "system",
                "passed": False,
                "output": str(e)
            })
            results["passed"] = False
        finally:
            # Clean up sandbox
            release_sandbox(sandbox_dir)

        return results

    @staticmethod
    def _scoped_aws_env(aws_credentials: Dict[str, Optional[str]], region: str) -> Dict[str, str]:
        """Minimal env for a single credentialed subprocess - never
        os.environ.copy(). Only PATH/HOME/TF_PLUGIN_CACHE_DIR plus the AWS
        credential vars; TF_LOG/TF_LOG_PATH forced unset (provider debug logs
        can write raw request bodies, credentials included, to disk)."""
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "TF_PLUGIN_CACHE_DIR": os.environ.get("TF_PLUGIN_CACHE_DIR", ""),
            "TF_IN_AUTOMATION": "1",
            "AWS_ACCESS_KEY_ID": aws_credentials.get("access_key") or "",
            "AWS_SECRET_ACCESS_KEY": aws_credentials.get("secret_key") or "",
            "AWS_DEFAULT_REGION": region,
        }
        if aws_credentials.get("session_token"):
            env["AWS_SESSION_TOKEN"] = aws_credentials["session_token"]
        env.pop("TF_LOG", None)
        env.pop("TF_LOG_PATH", None)
        return env

    @classmethod
    async def generate_config(
        cls,
        hcl_files: Dict[str, str],
        aws_credentials: Dict[str, Optional[str]],
        region: str,
        binary: str = "terraform",
    ) -> Dict[str, Any]:
        """Terraform's own view of the imported resources: a sandbox holding only
        versions/providers and the import {} blocks (no resource blocks), then
        `plan -generate-config-out=generated.tf`, which writes HCL for every
        import target from the live resource. Used to cross-check our generated
        attributes. Read-only: plan never writes state or touches AWS, and no
        `-out` plan file is produced. Same scoped-credential rules as plan_json."""
        sandbox_dir = create_sandbox(prefix="terraagent_genconfig_")
        result: Dict[str, Any] = {"passed": False, "generated": "", "checks": []}
        env = cls._scoped_aws_env(aws_credentials, region)
        keep = {"versions.tf", "providers.tf", "variables.tf", "locals.tf", "imports.tf"}
        try:
            for filename, content in hcl_files.items():
                if filename not in keep:
                    continue
                with open(os.path.join(sandbox_dir, filename), "w", encoding="utf-8") as f:
                    f.write(content)

            code, out, err = await cls.run_command(
                [binary, "init", "-backend=false", "-input=false"], cwd=sandbox_dir, env=env
            )
            result["checks"].append({"check_name": "init", "passed": code == 0,
                                     "output": CredentialScrubber.scrub_text(out + ("\n" + err if err else ""))})
            if code != 0:
                return result

            code, out, err = await cls.run_command(
                [binary, "plan", "-generate-config-out=generated.tf", "-input=false", "-lock=false", "-no-color"],
                cwd=sandbox_dir, env=env,
            )
            generated_path = os.path.join(sandbox_dir, "generated.tf")
            generated = ""
            if os.path.exists(generated_path):
                with open(generated_path, "r", encoding="utf-8") as f:
                    generated = f.read()
            # plan can exit non-zero after writing generated.tf (the generated
            # config may itself not validate); what matters here is the file.
            result["generated"] = CredentialScrubber.scrub_text(generated)
            result["passed"] = bool(generated)
            result["checks"].append({"check_name": "generate_config", "passed": bool(generated),
                                     "output": CredentialScrubber.scrub_text(out + ("\n" + err if err else ""))[-4000:]})
        except Exception as e:
            result["checks"].append({"check_name": "system", "passed": False,
                                     "output": CredentialScrubber.scrub_text(str(e))})
        finally:
            release_sandbox(sandbox_dir)
        return result

    @classmethod
    async def plan_json(
        cls,
        hcl_files: Dict[str, str],
        aws_credentials: Dict[str, Optional[str]],
        region: str,
        binary: str = "terraform",
    ) -> Dict[str, Any]:
        """Run a REAL `<binary> init && plan && show -json` against live AWS and
        tally create/update/replace/destroy/no-op actions from resource_changes.

        This is the one place in this codebase a real AWS provider handshake
        happens outside boto3 - unlike validate_hcl (schema-only, no network, no
        credentials needed), a meaningful plan against import {} blocks needs the
        provider to actually reach AWS. Two things make that safe to do here:

        1. Credentials are scoped to THIS subprocess call only, via an explicit
           env dict - never os.environ.copy(), which would blindly forward
           whatever else happens to be in the parent process's environment.
           Only PATH/HOME/TF_PLUGIN_CACHE_DIR (needed for the binary and its
           provider plugin cache to work at all) plus the AWS credential vars
           are included. TF_LOG/TF_LOG_PATH are explicitly forced unset even if
           somehow present upstream - Terraform provider debug logging can write
           raw request/response bodies, credentials included, to disk.
        2. Every check's stdout/stderr is scrubbed via CredentialScrubber before
           being placed into the returned dict, since that dict is what gets
           persisted into TerraAgentState (and from there, Redis/Postgres) -
           unlike the env dict itself, which is local to this call and never
           stored anywhere.

        `plan`/`show` are already permitted by run_command's disallowed-argv
        check (only apply/destroy/import are blocked) and neither ever mutates
        real infrastructure - `plan` only ever reads, and `-out` just writes a
        local plan file inside the sandbox that's destroyed with it.
        """
        sandbox_dir = create_sandbox(prefix="terraagent_plan_")
        result: Dict[str, Any] = {
            "passed": False,
            "skipped": False,
            "create": 0,
            "update": 0,
            "replace": 0,
            "destroy": 0,
            "no_op": 0,
            "imported": 0,  # resources bound by an import {} block
            "blocking_actions": [],
            "changes": [],  # every managed address whose action isn't no-op: {address, action}
            "checks": [],
        }

        env = cls._scoped_aws_env(aws_credentials, region)

        try:
            for filename, content in hcl_files.items():
                file_path = os.path.join(sandbox_dir, filename)
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

            code, out, err = await cls.run_command(
                [binary, "init", "-backend=false", "-input=false"], cwd=sandbox_dir, env=env
            )
            init_passed = code == 0
            result["checks"].append({
                "check_name": "init",
                "passed": init_passed,
                "output": CredentialScrubber.scrub_text(out + ("\n" + err if err else "")),
            })
            if not init_passed:
                return result

            code, out, err = await cls.run_command(
                [binary, "plan", "-out=tfplan", "-input=false", "-lock=false"], cwd=sandbox_dir, env=env
            )
            plan_passed = code == 0
            result["checks"].append({
                "check_name": "plan",
                "passed": plan_passed,
                "output": CredentialScrubber.scrub_text(out + ("\n" + err if err else "")),
            })
            if not plan_passed:
                return result

            code, out, err = await cls.run_command(
                [binary, "show", "-json", "tfplan"], cwd=sandbox_dir, env=env
            )
            if code != 0:
                result["checks"].append({
                    "check_name": "show",
                    "passed": False,
                    "output": CredentialScrubber.scrub_text(err or out),
                })
                return result

            plan_data = json.loads(out)
            resource_changes = plan_data.get("resource_changes", []) or []
            blocking: List[Dict[str, str]] = []
            for change in resource_changes:
                actions = change.get("change", {}).get("actions", [])
                address = change.get("address", "unknown")
                if change.get("change", {}).get("importing"):
                    result["imported"] += 1
                if actions in (["no-op"], ["read"]):
                    result["no_op"] += 1
                    continue
                if change.get("mode") == "data":
                    continue
                if actions == ["create"]:
                    result["create"] += 1
                    result["changes"].append({"address": address, "action": "create"})
                elif actions == ["update"]:
                    result["update"] += 1
                    result["changes"].append({"address": address, "action": "update"})
                elif "delete" in actions and "create" in actions:
                    result["replace"] += 1
                    blocking.append({"address": address, "action": "replace"})
                    result["changes"].append({"address": address, "action": "replace"})
                elif actions == ["delete"]:
                    result["destroy"] += 1
                    blocking.append({"address": address, "action": "destroy"})
                    result["changes"].append({"address": address, "action": "destroy"})

            result["blocking_actions"] = blocking
            result["passed"] = not blocking
            result["checks"].append({
                "check_name": "show",
                "passed": True,
                "output": f"{len(resource_changes)} resource_changes parsed from plan output.",
            })
        except Exception as e:
            result["checks"].append({
                "check_name": "system",
                "passed": False,
                "output": CredentialScrubber.scrub_text(str(e)),
            })
        finally:
            release_sandbox(sandbox_dir)

        return result

    @classmethod
    async def plan_saved(
        cls,
        workdir: str,
        target: Dict[str, Any],
        deployment_id: str,
        aws_credentials: Optional[Dict[str, str]] = None,
        region: str = "us-east-1",
        binary: str = "terraform",
    ) -> Dict[str, Any]:
        """Run `init` with target backend-config and `plan -out=tfplan` against live AWS
        for deployment mode (Phase 3). Returns the plan binary, redacted plan JSON,
        resource change tallies, and destructive flags.

        All subcommands pass through check_argv without exception.
        Credentials are scoped to this subprocess execution only and never logged.
        """
        result: Dict[str, Any] = {
            "passed": False,
            "counts": {
                "create": 0,
                "update": 0,
                "replace": 0,
                "destroy": 0,
                "no_op": 0,
            },
            "changes": [],
            "is_destructive": False,
            "plan_json": {},
            "raw_plan_json": {},
            "plan_binary": b"",
            "checks": [],
        }

        env = cls._scoped_aws_env(aws_credentials, region)
        target_id = target.get("id", "default")
        bucket = target.get("state_bucket", "")
        backend_key = f"terraagent/{target_id}/{deployment_id}.tfstate"

        try:
            if bucket:
                backend_tf = os.path.join(workdir, "_backend.tf")
                if not os.path.exists(backend_tf):
                    with open(backend_tf, "w", encoding="utf-8") as f:
                        f.write('terraform {\n  backend "s3" {}\n}\n')

            # 1. terraform init with remote S3 backend configuration
            init_cmd = [
                binary,
                "init",
                f"-backend-config=bucket={bucket}",
                f"-backend-config=key={backend_key}",
                f"-backend-config=region={region}",
                "-input=false",
            ]
            code, out, err = await cls.run_command(init_cmd, cwd=workdir, env=env)
            init_passed = code == 0
            result["checks"].append({
                "check_name": "init",
                "passed": init_passed,
                "output": CredentialScrubber.scrub_text(out + ("\n" + err if err else "")),
            })
            if not init_passed:
                return result

            # 2. terraform plan -out=tfplan -lock=false (read-only plan role)
            plan_cmd = [binary, "plan", "-out=tfplan", "-input=false", "-lock=false"]
            code, out, err = await cls.run_command(plan_cmd, cwd=workdir, env=env)
            plan_passed = code == 0
            result["checks"].append({
                "check_name": "plan",
                "passed": plan_passed,
                "output": CredentialScrubber.scrub_text(out + ("\n" + err if err else "")),
            })
            if not plan_passed:
                return result

            # 3. terraform show -json tfplan
            show_cmd = [binary, "show", "-json", "tfplan"]
            code, out, err = await cls.run_command(show_cmd, cwd=workdir, env=env)
            if code != 0:
                result["checks"].append({
                    "check_name": "show",
                    "passed": False,
                    "output": CredentialScrubber.scrub_text(err or out),
                })
                return result

            # 4. Read plan binary
            plan_path = os.path.join(workdir, "tfplan")
            if os.path.exists(plan_path):
                with open(plan_path, "rb") as f:
                    result["plan_binary"] = f.read()

            raw_plan_data = json.loads(out)
            result["raw_plan_json"] = raw_plan_data
            result["plan_json"] = json.loads(CredentialScrubber.scrub_text(out))

            # 5. Parse resource_changes
            resource_changes = raw_plan_data.get("resource_changes", []) or []
            counts = result["counts"]
            destructive_changes = []

            for change in resource_changes:
                actions = change.get("change", {}).get("actions", [])
                address = change.get("address", "unknown")
                res_type = change.get("type", "unknown")

                if actions in (["no-op"], ["read"]):
                    counts["no_op"] += 1
                    continue
                if change.get("mode") == "data":
                    continue

                if actions == ["create"]:
                    counts["create"] += 1
                    result["changes"].append({"address": address, "action": "create", "type": res_type})
                elif actions == ["update"]:
                    counts["update"] += 1
                    result["changes"].append({"address": address, "action": "update", "type": res_type})
                elif "delete" in actions and "create" in actions:
                    counts["replace"] += 1
                    destructive_changes.append({"address": address, "action": "replace", "type": res_type})
                    result["changes"].append({"address": address, "action": "replace", "type": res_type})
                elif actions == ["delete"]:
                    counts["destroy"] += 1
                    destructive_changes.append({"address": address, "action": "destroy", "type": res_type})
                    result["changes"].append({"address": address, "action": "destroy", "type": res_type})

            result["is_destructive"] = bool(counts["replace"] > 0 or counts["destroy"] > 0)
            result["passed"] = True
            result["checks"].append({
                "check_name": "show",
                "passed": True,
                "output": f"{len(resource_changes)} resource_changes analyzed.",
            })
        except Exception as e:
            result["checks"].append({
                "check_name": "system",
                "passed": False,
                "output": CredentialScrubber.scrub_text(str(e)),
            })

        return result

