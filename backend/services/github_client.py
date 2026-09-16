"""Lightweight GitHub REST API client for opening an adoption Pull Request.

Uses direct httpx calls against the GitHub REST API - consistent with this
codebase's established preference (see services/webhook.py,
services/ollama_client.py: no SDK dependency for a handful of HTTP calls).
GITHUB_API_BASE is a hardcoded constant, never accepted as input, so there's
no user-controlled URL here the way services/webhook.py's webhook_url is -
that file's IP-pinning/SSRF defense doesn't apply because there's nothing
for a caller to redirect this client to.

The GitHub token itself never appears in this module's return values, logs,
or exceptions beyond the Authorization header of the outgoing request -
callers (routers/scan.py) extract it from a SecretStr request field, pass
it straight through here, and it is discarded once this function returns.
It is never written into TerraAgentState, Redis, or any log line.
"""

import base64
import logging
from typing import Any, Dict, List, Optional

import httpx

from tools.hcl_blocks import STACK_FILE_NAMES, extract_resource_block
from tools.naming import unique_clean_name

logger = logging.getLogger("terraagent.github_client")

GITHUB_API_BASE = "https://api.github.com"
_REQUEST_TIMEOUT = 20.0


class GitHubPullRequestError(RuntimeError):
    """Raised for any non-2xx GitHub API response. The message never
    includes the token (it's never present in a response body); commit/PR
    content itself CAN legitimately appear in a raw API error message
    (e.g. GitHub echoing back an invalid path), so callers should still
    scrub before logging/returning it to an HTTP client, same discipline as
    every other externally-sourced error string in this codebase."""


async def _existing_file_sha(client: httpx.AsyncClient, repo: str, path: str, ref: str) -> Optional[str]:
    """Returns the blob sha of `path` on `ref` if it already exists there,
    None if it doesn't. The Contents API's PUT (create-or-update-file)
    endpoint requires this sha to overwrite an existing file - omitting it
    when one is present returns a 422, which is exactly what happened before
    this check existed, on the very first commit (README.md) against almost
    any real target repo."""
    resp = await client.get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
    if resp.status_code == 200:
        data = resp.json()
        if isinstance(data, dict):
            return data.get("sha")
    return None


def _import_cmd(res: Dict[str, Any]) -> str:
    r_type = res.get("resource_type")
    r_id = res.get("id")
    clean_name = unique_clean_name(res.get("name", r_id), r_id)
    return f"terraform import {r_type}.{clean_name} {r_id}"


def _extract_wave_files(
    tf_files: Dict[str, str], resource_ids: List[str], resources_by_id: Dict[str, Dict[str, Any]]
) -> Dict[str, str]:
    """Filters tf_files down to just the resource blocks for resource_ids -
    scopes a PR to a single adoption wave instead of the whole job. Stack
    files (STACK_FILE_NAMES) are rebuilt containing only the matching
    blocks, via the exact same balanced-brace extraction repair_agent.py
    uses to pull a single failing block for LLM repair. Every other file
    (providers.tf, variables.tf, outputs.tf - no per-resource identity) is
    passed through unchanged, since a wave's extracted blocks aren't valid
    standalone HCL without them - every wave's PR needs its own copy.
    """
    filtered: Dict[str, str] = {}
    for filename, content in tf_files.items():
        if filename not in STACK_FILE_NAMES:
            filtered[filename] = content
            continue

        blocks = []
        for rid in resource_ids:
            res = resources_by_id.get(rid)
            resource_type = res.get("resource_type") if res else None
            if not res or not resource_type:
                continue
            clean_name = unique_clean_name(res.get("name", rid), rid)
            block = extract_resource_block(content, resource_type, clean_name)
            if block:
                blocks.append(block)

        if blocks:
            filtered[filename] = "\n\n".join(blocks) + "\n"

    return filtered


def _build_pr_body(
    job_id: str,
    adoption_plan: Dict[str, Any],
    plan_equivalence_results: Dict[str, Any],
    drift_results: Dict[str, Any],
    security_results: Dict[str, Any],
    cost_results: Dict[str, Any],
    pending_approval: Optional[Dict[str, Any]],
    approval_decision: Optional[Dict[str, Any]],
    wave: Optional[Dict[str, Any]] = None,
    wave_import_cmds: Optional[List[str]] = None,
) -> str:
    if wave:
        lines: List[str] = [
            f"## TerraAgent Adoption PR - Wave {wave.get('wave')} - Job `{job_id}`",
            "",
            f"Part of a {adoption_plan.get('total_resource_count', 0)}-resource adoption plan "
            f"({len(adoption_plan.get('waves') or [])} wave(s) total) - this PR covers only wave "
            f"{wave.get('wave')}: {len(wave.get('resource_ids') or [])} resource(s), risk level "
            f"**{wave.get('risk_level', 'unknown')}**.",
        ]
        if wave.get("risk_signals"):
            lines.append("")
            lines.append("**Risk signals for this wave:**")
            for signal in wave["risk_signals"]:
                lines.append(f"- {signal}")
    else:
        lines = [
            f"## TerraAgent Adoption PR - Job `{job_id}`",
            "",
            "Auto-generated by TerraAgent from a real, read-only AWS discovery scan. "
            "Review carefully before merging - see \"Before Merging\" below.",
            "",
            "### Adoption Summary",
            f"- **Total discovered resources:** {adoption_plan.get('total_resource_count', 0)}",
            f"- **Adoption risk score:** {adoption_plan.get('risk_score', 0)} / 100",
        ]
        if adoption_plan.get("summary"):
            lines.append(f"- **Summary:** {adoption_plan['summary']}")

    lines += ["", "### Drift Findings (live AWS vs. the generated configuration)"]
    if drift_results.get("skipped"):
        lines.append(f"- Not run: {drift_results.get('reason', 'skipped')}")
    else:
        lines.append(
            f"- {drift_results.get('resources_checked', 0)} resource(s) checked, "
            f"{drift_results.get('resources_missing', 0)} no longer exist, "
            f"{drift_results.get('destructive_equivalent_count', 0)} destructive-equivalent, "
            f"{drift_results.get('behavior_changing_count', 0)} behavior-changing, "
            f"{drift_results.get('informational_count', 0)} informational (tag) difference(s)"
        )
        for f in (drift_results.get("findings") or [])[:20]:
            if f.get("tier") == "informational":
                continue
            lines.append(f"  - **[{f.get('tier')}]** `{f.get('resource')}` / `{f.get('attribute')}`: {f.get('description', '')}")

    lines += ["", "### Plan Equivalence (real `terraform plan` against live AWS)"]
    if plan_equivalence_results.get("skipped"):
        lines.append(f"- Not run: {plan_equivalence_results.get('reason', 'skipped')}")
    else:
        lines.append(
            f"- create: {plan_equivalence_results.get('create', 0)}, "
            f"update: {plan_equivalence_results.get('update', 0)}, "
            f"replace: {plan_equivalence_results.get('replace', 0)}, "
            f"destroy: {plan_equivalence_results.get('destroy', 0)}, "
            f"no-op: {plan_equivalence_results.get('no_op', 0)}"
        )

    lines += ["", "### Security Findings"]
    findings = security_results.get("findings") or []
    lines.append(f"- **Risk score:** {security_results.get('risk_score', 0)} / 100")
    lines.append(
        f"- **Critical:** {security_results.get('critical_count', 0)}, "
        f"**High:** {security_results.get('high_count', 0)}"
    )
    scanners_skipped = security_results.get("scanners_skipped") or []
    if scanners_skipped:
        lines.append(
            f"- ⚠️ Scanners that did not run: {', '.join(scanners_skipped)} "
            "(not reflected in the score above)"
        )
    if findings:
        lines += ["", "| Tool | Rule | Severity | Resource |", "|---|---|---|---|"]
        for f in findings[:20]:
            lines.append(f"| {f.get('tool', '')} | {f.get('rule_id', '')} | {f.get('severity', '')} | `{f.get('resource', '')}` |")
        if len(findings) > 20:
            lines.append(f"| ... | *{len(findings) - 20} more finding(s) - see the full scan results* | | |")

    if pending_approval and pending_approval.get("findings"):
        lines += ["", "### ⚠️ Findings That Required Human Approval"]
        if approval_decision:
            decision = approval_decision.get("decision", "unknown")
            reason = approval_decision.get("reason")
            lines.append(
                f"A human **{decision.upper()}** this configuration despite the finding(s) below"
                + (f": *{reason}*" if reason else ".")
            )
        else:
            lines.append("These were deliberately left unresolved by the automated pipeline - verify before merging:")
        for f in pending_approval["findings"]:
            lines.append(f"- **[{f.get('tier', 'unknown')}]** `{f.get('resource', 'unknown')}`: {f.get('description', '')}")

    lines += ["", "### Estimated Monthly Cost"]
    if cost_results.get("tool_skipped"):
        lines.append("- Not estimated (Infracost was unavailable for this run)")
    else:
        lines.append(f"- ${cost_results.get('total_monthly_cost', 0.0):.2f} {cost_results.get('currency', 'USD')}/month")

    lines += ["", "### Before Merging", "1. Run `terraform init` inside `terraform/`."]
    if wave_import_cmds:
        lines.append(
            "2. Run these `terraform import` commands, in order - this links existing AWS "
            "resources to state without creating duplicates:"
        )
        lines.append("```bash")
        lines.extend(wave_import_cmds)
        lines.append("```")
    else:
        lines.append(
            "2. Run every `terraform import` command in `migration/import_plan.md`, in order - "
            "this links existing AWS resources to state without creating duplicates."
        )
    lines += [
        "3. Run `terraform plan` and confirm it reports `No changes.`",
        "4. Only merge and apply once the plan is clean and this PR has been reviewed. "
        "TerraAgent itself never runs `terraform apply` or `terraform destroy`.",
    ]
    return "\n".join(lines)


async def _create_branch(client: httpx.AsyncClient, repo: str, branch_name: str, base_sha: str) -> None:
    create_ref_resp = await client.post(
        f"/repos/{repo}/git/refs",
        json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
    )
    if create_ref_resp.status_code == 422:
        # Most likely a stray branch left behind by an earlier failed
        # attempt for this exact job/wave - self-heal by deleting it and
        # trying again, rather than permanently blocking every future retry.
        await client.delete(f"/repos/{repo}/git/refs/heads/{branch_name}")
        create_ref_resp = await client.post(
            f"/repos/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        )
    if create_ref_resp.status_code not in (200, 201):
        raise GitHubPullRequestError(
            f"Failed to create branch '{branch_name}' on '{repo}': "
            f"{create_ref_resp.status_code} {create_ref_resp.text}"
        )


async def create_adoption_pr(
    github_token: str,
    repo: str,
    job_id: str,
    tf_files: Dict[str, str],
    docs: Dict[str, str],
    adoption_plan: Dict[str, Any],
    plan_equivalence_results: Dict[str, Any],
    security_results: Dict[str, Any],
    cost_results: Dict[str, Any],
    pending_approval: Optional[Dict[str, Any]],
    approval_decision: Optional[Dict[str, Any]],
    drift_results: Optional[Dict[str, Any]] = None,
    resources: Optional[List[Dict[str, Any]]] = None,
    wave: Optional[Dict[str, Any]] = None,
    base_branch: str = "main",
) -> Dict[str, Any]:
    """Creates a branch off base_branch, commits the generated Terraform
    files (plus the migration/README docs) onto it via the Contents API - one
    real commit per file, the same approach this codebase's own plan
    document for this feature calls for - then opens a PR against
    base_branch with an adoption/plan-equivalence/security summary in the
    body. Raises GitHubPullRequestError on any non-2xx response.

    Two things verified against GitHub's actual, documented Contents API
    contract that a naive implementation (and this function's own first
    version) gets wrong:

    1. `PUT /repos/{owner}/{repo}/contents/{path}` requires the existing
       file's blob `sha` when a file already exists at that path (a plain
       422 otherwise) - and since the new branch is forked from base_sha, it
       inherits everything already in the target repo. Every one of these
       PRs commits a README.md, and virtually every real GitHub repo already
       has one, so this fired on the very first commit against almost any
       real target - `_existing_file_sha` below checks for and supplies it.
    2. If anything fails after the branch is created (a commit, or the PR-
       open call itself), the branch must be deleted before the error
       propagates - otherwise it's left behind half-populated, and since the
       branch name is deterministic, retrying the exact same job/wave would
       fail immediately with "Reference already exists" on the very next
       attempt's branch-create call, forever, until someone deleted it by
       hand on GitHub. Symmetrically, if a stray branch from a PRE-fix run
       (or any other reason) is already sitting there when this function
       starts, it's deleted and recreated rather than treated as a hard
       failure.

    When `wave` is given (one entry from adoption_plan["waves"]), this scopes
    the PR to just that wave instead of the whole job: `tf_files` is filtered
    down to only that wave's resource blocks (plus shared foundational files
    like providers.tf/variables.tf, which aren't per-resource so every wave
    needs its own copy) via `_extract_wave_files`, the job-wide `docs` bundle
    is left uncommitted (README.md/import_plan.md describe the WHOLE
    adoption, which would be misleading committed alongside a single wave's
    files), and the PR body carries a wave-scoped summary plus this wave's
    own `terraform import` commands inline instead of pointing at
    migration/import_plan.md. `resources` (the raw discovered-resource list)
    is required in this case, to resolve each wave resource's type/clean
    name for both block extraction and the import commands.
    """
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    wave_import_cmds: Optional[List[str]] = None
    if wave:
        if resources is None:
            raise ValueError("resources is required when wave is given")
        resources_by_id = {r["id"]: r for r in resources if r.get("id")}
        wave_ids = wave.get("resource_ids") or []
        files_to_commit_base = _extract_wave_files(tf_files, wave_ids, resources_by_id)
        docs_to_commit: Dict[str, str] = {}
        wave_import_cmds = [_import_cmd(resources_by_id[rid]) for rid in wave_ids if rid in resources_by_id]
        branch_name = f"terraagent/adopt-{job_id}-wave-{wave.get('wave')}"
        pr_title = (
            f"TerraAgent: adopt wave {wave.get('wave')} "
            f"({wave.get('risk_level', 'unknown')} risk, {len(wave_ids)} resources) - job {job_id}"
        )
    else:
        files_to_commit_base = dict(tf_files)
        docs_to_commit = dict(docs)
        branch_name = f"terraagent/adopt-{job_id}"
        pr_title = f"TerraAgent: adopt discovered infrastructure ({job_id})"

    async with httpx.AsyncClient(base_url=GITHUB_API_BASE, headers=headers, timeout=_REQUEST_TIMEOUT) as client:
        ref_resp = await client.get(f"/repos/{repo}/git/ref/heads/{base_branch}")
        if ref_resp.status_code != 200:
            raise GitHubPullRequestError(
                f"Failed to read base branch '{base_branch}' on '{repo}': "
                f"{ref_resp.status_code} {ref_resp.text}"
            )
        base_sha = ref_resp.json()["object"]["sha"]

        await _create_branch(client, repo, branch_name, base_sha)

        try:
            files_to_commit: Dict[str, str] = {f"terraform/{name}": content for name, content in files_to_commit_base.items()}
            files_to_commit.update(docs_to_commit)

            for path, content in files_to_commit.items():
                encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
                payload: Dict[str, Any] = {
                    "message": f"TerraAgent: add {path} (job {job_id})",
                    "content": encoded,
                    "branch": branch_name,
                }
                existing_sha = await _existing_file_sha(client, repo, path, branch_name)
                if existing_sha:
                    payload["sha"] = existing_sha

                put_resp = await client.put(f"/repos/{repo}/contents/{path}", json=payload)
                if put_resp.status_code not in (200, 201):
                    raise GitHubPullRequestError(
                        f"Failed to commit '{path}' to '{repo}@{branch_name}': "
                        f"{put_resp.status_code} {put_resp.text}"
                    )

            pr_body = _build_pr_body(
                job_id, adoption_plan, plan_equivalence_results, drift_results or {}, security_results,
                cost_results, pending_approval, approval_decision,
                wave=wave, wave_import_cmds=wave_import_cmds,
            )
            pr_resp = await client.post(
                f"/repos/{repo}/pulls",
                json={
                    "title": pr_title,
                    "head": branch_name,
                    "base": base_branch,
                    "body": pr_body,
                },
            )
            if pr_resp.status_code not in (200, 201):
                raise GitHubPullRequestError(
                    f"Failed to open pull request on '{repo}': {pr_resp.status_code} {pr_resp.text}"
                )
        except GitHubPullRequestError:
            # Never leave a half-populated branch behind - a retry of this
            # exact job/wave must be able to start clean, not hit "Reference
            # already exists" on its very first call.
            try:
                await client.delete(f"/repos/{repo}/git/refs/heads/{branch_name}")
            except Exception:
                pass
            raise

        pr_data = pr_resp.json()
        logger.info(f"[{job_id}] Opened GitHub PR #{pr_data.get('number')} on {repo}")
        return {
            "pr_url": pr_data.get("html_url"),
            "pr_number": pr_data.get("number"),
            "branch": branch_name,
            "wave": wave.get("wave") if wave else None,
        }
