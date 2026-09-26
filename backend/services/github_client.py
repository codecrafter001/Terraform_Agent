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
from tools.hcl_generator import association_address, route_table_subnets
from tools.import_blocks import IMPORTS_FILENAME, filter_imports_tf
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


def _handle_api_error(resp: httpx.Response, action_desc: str) -> GitHubPullRequestError:
    """Produces descriptive, user-friendly error messages based on GitHub status codes."""
    status = resp.status_code
    try:
        data = resp.json()
        raw_msg = data.get("message", resp.text)
    except Exception:
        raw_msg = resp.text

    prefix = f"Failed {action_desc}: {status}"
    if status == 401:
        return GitHubPullRequestError(f"{prefix} (401 Unauthorized) - {raw_msg}. Please verify your GitHub Personal Access Token (PAT).")
    elif status == 403:
        return GitHubPullRequestError(f"{prefix} (403 Forbidden) - {raw_msg}. Check token permissions (needs 'repo' scope) or API rate limits.")
    elif status == 404:
        return GitHubPullRequestError(f"{prefix} (404 Not Found) - {raw_msg}. Check the repository format (owner/repo).")
    elif status in (405, 409):
        return GitHubPullRequestError(f"{prefix} ({status} Conflict) - {raw_msg}. PR may have merge conflicts, not be mergeable, or be blocked by branch protection rules.")
    elif status == 422:
        return GitHubPullRequestError(f"{prefix} (422 Unprocessable) - {raw_msg}. Branch or changes may already exist or contain invalid parameters.")
    return GitHubPullRequestError(f"{prefix} - {raw_msg}")


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
    wave_addresses = []
    wave_blocks: List[tuple] = []  # (resource type, name) of every block this wave owns
    for rid in resource_ids:
        res = resources_by_id.get(rid)
        if res and res.get("resource_type"):
            name = unique_clean_name(res.get("name", rid), rid)
            wave_addresses.append(f"{res['resource_type']}.{name}")
            wave_blocks.append((res["resource_type"], name))
            if res["resource_type"] == "aws_route_table":
                # A route table's subnet associations travel with it.
                for idx in range(len(route_table_subnets(res))):
                    assoc = association_address(name, idx)
                    wave_addresses.append(assoc)
                    wave_blocks.append(tuple(assoc.split(".", 1)))

    for filename, content in tf_files.items():
        if filename == IMPORTS_FILENAME:
            # Import blocks must only target resources this PR contains, or
            # `terraform validate` fails ("import target does not exist").
            wave_imports = filter_imports_tf(content, wave_addresses)
            if wave_imports:
                filtered[filename] = wave_imports
            continue
        if filename not in STACK_FILE_NAMES:
            filtered[filename] = content
            continue

        blocks = []
        for resource_type, name in wave_blocks:
            block = extract_resource_block(content, resource_type, name)
            if block:
                blocks.append(block)

        if blocks:
            filtered[filename] = "\n\n".join(blocks) + "\n"

    return filtered


def _md(text: Any) -> str:
    """Discovered text in a markdown table cell: no pipes or newlines."""
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ").replace("`", "'")


def _scores_section(migration_safety: Optional[Dict[str, Any]], security_posture: Optional[Dict[str, Any]]) -> List[str]:
    lines = ["", "### Scores (kept separate on purpose)"]
    s = migration_safety or {}
    score = f"{s['score']}%" if s.get("score") is not None else "not measured"
    lines.append(
        f"- **Migration Safety:** {score} - {s.get('status', 'UNVERIFIED')} "
        f"(basis: {s.get('basis', 'none')}; {s.get('destroy_or_replace', 0)} destroy/replace). "
        "Will adopting this change anything?"
    )
    p = security_posture or {}
    posture = f"{p['score']}/100" if p.get("score") is not None else "not measured"
    lines.append(
        f"- **Security Posture:** {posture} - {p.get('rating', 'UNKNOWN')}"
        f"{'' if p.get('complete', True) else ' (partial: not every scanner completed)'}. "
        "What is wrong with the current setup? Reported only - fixes go in the separate Hardening PR."
    )
    return lines


def _resource_table(infra_model: Optional[Dict[str, Any]], plan: Dict[str, Any], limit: int = 60) -> List[str]:
    records = (infra_model or {}).get("records") or []
    managed = [r for r in records if r.get("decision") == "manage"]
    if not managed:
        return []
    planned = bool(plan) and not plan.get("skipped") and any(
        c.get("check_name") == "show" and c.get("passed") for c in plan.get("checks", []) or [])
    actions = {c.get("address"): c.get("action") for c in plan.get("changes", []) or []}
    lines = ["", "### Resources in this PR", "| Address | Import ID | Plan |", "|---|---|---|"]
    for r in managed[:limit]:
        address = f"{r.get('type')}.{unique_clean_name(r.get('name') or r.get('id'), r.get('id'))}"
        action = actions.get(address, "no-op") if planned else "not planned"
        lines.append(f"| `{_md(address)}` | `{_md(r.get('import_id'))}` | {action} |")
    if len(managed) > limit:
        lines.append(f"| ... | *{len(managed) - limit} more - see infra_model.json* | |")
    return lines


def _decisions_section(infra_model: Optional[Dict[str, Any]]) -> List[str]:
    model = infra_model or {}
    summary = model.get("summary") or {}
    if not summary:
        return []
    lines = ["", "### Classification decisions",
             f"- manage **{summary.get('manage', 0)}**, reference **{summary.get('reference', 0)}**, "
             f"exclude **{summary.get('exclude', 0)}**, review **{summary.get('review', 0)}**"]
    others = [r for r in model.get("records") or [] if r.get("decision") != "manage"][:30]
    if others:
        lines += ["", "| Resource | Decision | Why |", "|---|---|---|"]
        for r in others:
            lines.append(f"| `{_md(r.get('type'))}` {_md(r.get('id'))} | {r.get('decision')} | "
                         f"{_md('; '.join(r.get('reasons') or []))} |")
    return lines


PIPELINE_NOTES = [
    "", "### Running it in your pipeline",
    "All code is under `terraform/`. TerraAgent never applies anything - your pipeline does:",
    "- **Atlantis:** autoplan picks up `terraform/` on this PR (default project detection).",
    "- **HCP Terraform:** point a VCS-driven workspace's working directory at `terraform/`.",
    "- **Spacelift:** set the stack's project root to `terraform/`.",
]


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
    migration_safety: Optional[Dict[str, Any]] = None,
    security_posture: Optional[Dict[str, Any]] = None,
    infra_model: Optional[Dict[str, Any]] = None,
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
            "Brings existing AWS resources under Terraform **exactly as they are**: every managed resource "
            "has an `import {}` block, and the code is meant to plan with **zero changes**. Security "
            "findings are reported below, never fixed here - fixes come in a separate Hardening PR. "
            "Generated from a read-only discovery scan; review before merging (see \"Before Merging\").",
            "",
            "### Adoption Summary",
            f"- **Total discovered resources:** {adoption_plan.get('total_resource_count', 0)}",
            f"- **Adoption risk score:** {adoption_plan.get('risk_score', 0)} / 100",
        ]
        if adoption_plan.get("summary"):
            lines.append(f"- **Summary:** {adoption_plan['summary']}")
        lines += _scores_section(migration_safety, security_posture)
        lines += _resource_table(infra_model, plan_equivalence_results)
        lines += _decisions_section(infra_model)

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

    lines += ["", "### Cost Impact",
              "- None: this PR imports existing resources without changing them (any cost delta belongs to "
              "the Hardening PR)."]

    lines += PIPELINE_NOTES
    lines += ["", "### Before Merging", "1. Run `terraform init` inside `terraform/`."]
    lines.append(
        "2. `terraform/imports.tf` binds every managed resource to the existing AWS resource "
        "with an `import {}` block - keep it. Your pipeline's `terraform plan` should show each "
        "one as \"will be imported\"."
    )
    if wave_import_cmds:
        lines.append("   Terraform < 1.5 only: delete `imports.tf` and run these instead, in order:")
        lines.append("```bash")
        lines.extend(wave_import_cmds)
        lines.append("```")
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
    migration_safety: Optional[Dict[str, Any]] = None,
    security_posture: Optional[Dict[str, Any]] = None,
    infra_model: Optional[Dict[str, Any]] = None,
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
            last_commit_sha: Optional[str] = None

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
                    raise _handle_api_error(put_resp, f"committing '{path}' to '{repo}@{branch_name}'")
                try:
                    put_json = put_resp.json()
                    if isinstance(put_json, dict) and "commit" in put_json:
                        last_commit_sha = put_json["commit"].get("sha")
                except Exception:
                    pass

            pr_body = _build_pr_body(
                job_id, adoption_plan, plan_equivalence_results, drift_results or {}, security_results,
                cost_results, pending_approval, approval_decision,
                wave=wave, wave_import_cmds=wave_import_cmds,
                migration_safety=migration_safety, security_posture=security_posture, infra_model=infra_model,
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
                raise _handle_api_error(pr_resp, f"opening pull request on '{repo}'")
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
            "pr_title": pr_title,
            "branch": branch_name,
            "repo": repo,
            "base_branch": base_branch,
            "commit_sha": last_commit_sha or pr_data.get("head", {}).get("sha"),
            "changed_files": list(files_to_commit.keys()),
            "wave": wave.get("wave") if wave else None,
            "status": pr_data.get("state", "open"),
            "created_at": pr_data.get("created_at"),
        }


def _build_hardening_body(job_id: str, hardening: Dict[str, Any], adoption_pr: Dict[str, Any]) -> str:
    cost = hardening.get("cost") or {}
    if cost.get("tool_skipped") or cost.get("monthly_delta") is None:
        cost_line = "Not estimated (Infracost unavailable) - this is not $0."
    else:
        cost_line = f"{cost['monthly_delta']:+.2f} {cost.get('currency', 'USD')}/month"
    lines = [
        f"## TerraAgent Hardening PR - Job `{job_id}`",
        "",
        f"Optional security fixes on top of the adoption PR (#{adoption_pr.get('pr_number')}). "
        "**Merge and apply the adoption PR first.** Unlike the adoption, every change here alters live "
        "behavior when applied - review each one.",
        "",
        f"- **Monthly cost delta:** {cost_line}",
        f"- **Validated:** {'yes (terraform validate)' if hardening.get('validated') else 'no'}",
        "",
        "### Changes",
    ]
    for c in hardening.get("changes") or []:
        lines += [
            f"#### {c.get('title')}",
            f"- **Impact:** {c.get('impact', '').replace('_', ' ')}",
            f"- **What it does:** {c.get('explanation')}",
            f"- **Risk:** {c.get('risk')}",
        ]
        if c.get("findings"):
            lines.append(f"- **Resolves:** {', '.join(str(f) for f in c['findings'])}")
    recs = hardening.get("recommendations") or []
    if recs:
        lines += ["", "### Not fixed automatically - manual follow-up", "| Severity | Rule | Resource |", "|---|---|---|"]
        for r in recs[:30]:
            lines.append(f"| {r.get('severity', '')} | {_md(r.get('rule_id'))} | `{_md(r.get('resource'))}` |")
    lines += PIPELINE_NOTES
    lines += ["", "TerraAgent never runs `terraform apply`; your pipeline applies this after review."]
    return "\n".join(lines)


async def create_hardening_pr(
    github_token: str,
    repo: str,
    job_id: str,
    hardening: Dict[str, Any],
    adoption_pr: Dict[str, Any],
) -> Dict[str, Any]:
    """Opens the Hardening PR stacked on the adoption PR's branch, so its diff
    shows only the hardening changes. Same branch/commit/cleanup discipline
    and token handling as create_adoption_pr."""
    files = hardening.get("files") or {}
    adoption_branch = adoption_pr.get("branch")
    if not files or not adoption_branch:
        raise ValueError("hardening files and an adoption PR branch are required")
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    branch_name = f"terraagent/harden-{job_id}"
    last_commit_sha: Optional[str] = None
    async with httpx.AsyncClient(base_url=GITHUB_API_BASE, headers=headers, timeout=_REQUEST_TIMEOUT) as client:
        ref_resp = await client.get(f"/repos/{repo}/git/ref/heads/{adoption_branch}")
        if ref_resp.status_code != 200:
            raise _handle_api_error(ref_resp, f"reading adoption branch '{adoption_branch}' on '{repo}'")
        await _create_branch(client, repo, branch_name, ref_resp.json()["object"]["sha"])
        try:
            for name, content in files.items():
                path = f"terraform/{name}"
                payload: Dict[str, Any] = {
                    "message": f"TerraAgent hardening: update {path} (job {job_id})",
                    "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                    "branch": branch_name,
                }
                existing_sha = await _existing_file_sha(client, repo, path, branch_name)
                if existing_sha:
                    payload["sha"] = existing_sha
                put_resp = await client.put(f"/repos/{repo}/contents/{path}", json=payload)
                if put_resp.status_code not in (200, 201):
                    raise _handle_api_error(put_resp, f"committing '{path}' to '{repo}@{branch_name}'")
                try:
                    put_json = put_resp.json()
                    if isinstance(put_json, dict) and "commit" in put_json:
                        last_commit_sha = put_json["commit"].get("sha")
                except Exception:
                    pass
            pr_resp = await client.post(f"/repos/{repo}/pulls", json={
                "title": f"TerraAgent: security hardening ({len(hardening.get('changes') or [])} changes) - job {job_id}",
                "head": branch_name,
                "base": adoption_branch,
                "body": _build_hardening_body(job_id, hardening, adoption_pr),
            })
            if pr_resp.status_code not in (200, 201):
                raise _handle_api_error(pr_resp, f"opening hardening PR on '{repo}'")
        except GitHubPullRequestError:
            try:
                await client.delete(f"/repos/{repo}/git/refs/heads/{branch_name}")
            except Exception:
                pass
            raise
        pr_data = pr_resp.json()
        logger.info(f"[{job_id}] Opened hardening PR #{pr_data.get('number')} on {repo}")
        return {
            "pr_url": pr_data.get("html_url"),
            "pr_number": pr_data.get("number"),
            "branch": branch_name,
            "repo": repo,
            "base_branch": adoption_branch,
            "commit_sha": last_commit_sha or pr_data.get("head", {}).get("sha"),
            "changed_files": [f"terraform/{k}" for k in files.keys()],
            "wave": None,
            "kind": "hardening",
            "status": pr_data.get("state", "open"),
        }


async def get_pr_details(
    github_token: str,
    repo: str,
    pr_number: int,
) -> Dict[str, Any]:
    """Fetches real-time GitHub PR metadata, state, changed files, and reviews."""
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(base_url=GITHUB_API_BASE, headers=headers, timeout=_REQUEST_TIMEOUT) as client:
        pr_resp = await client.get(f"/repos/{repo}/pulls/{pr_number}")
        if pr_resp.status_code != 200:
            raise _handle_api_error(pr_resp, f"fetching PR #{pr_number} from '{repo}'")
        pr_data = pr_resp.json()

        # Fetch changed files
        files_resp = await client.get(f"/repos/{repo}/pulls/{pr_number}/files", params={"per_page": 100})
        files_data = files_resp.json() if files_resp.status_code == 200 else []

        changed_files_list = []
        if isinstance(files_data, list):
            for f in files_data:
                changed_files_list.append({
                    "filename": f.get("filename"),
                    "status": f.get("status"),
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                    "changes": f.get("changes", 0),
                    "patch": f.get("patch"),
                    "raw_url": f.get("raw_url"),
                })

        # Fetch reviews
        reviews_resp = await client.get(f"/repos/{repo}/pulls/{pr_number}/reviews")
        reviews_data = reviews_resp.json() if reviews_resp.status_code == 200 else []
        reviews_list = []
        if isinstance(reviews_data, list):
            for r in reviews_data:
                reviews_list.append({
                    "id": r.get("id"),
                    "user": (r.get("user") or {}).get("login"),
                    "state": r.get("state"),
                    "submitted_at": r.get("submitted_at"),
                    "body": r.get("body"),
                })

        return {
            "pr_number": pr_data.get("number"),
            "title": pr_data.get("title"),
            "state": pr_data.get("state"),
            "html_url": pr_data.get("html_url"),
            "body": pr_data.get("body"),
            "head_branch": (pr_data.get("head") or {}).get("ref"),
            "base_branch": (pr_data.get("base") or {}).get("ref"),
            "head_sha": (pr_data.get("head") or {}).get("sha"),
            "mergeable": pr_data.get("mergeable"),
            "mergeable_state": pr_data.get("mergeable_state"),
            "merged": pr_data.get("merged", False),
            "merged_at": pr_data.get("merged_at"),
            "merge_commit_sha": pr_data.get("merge_commit_sha"),
            "additions": pr_data.get("additions", 0),
            "deletions": pr_data.get("deletions", 0),
            "changed_files_count": pr_data.get("changed_files", len(changed_files_list)),
            "changed_files": changed_files_list,
            "reviews": reviews_list,
            "created_at": pr_data.get("created_at"),
            "updated_at": pr_data.get("updated_at"),
        }


async def get_pr_diff(
    github_token: str,
    repo: str,
    pr_number: int,
) -> str:
    """Fetches the raw unified git diff from GitHub for the pull request."""
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3.diff",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(base_url=GITHUB_API_BASE, headers=headers, timeout=_REQUEST_TIMEOUT) as client:
        diff_resp = await client.get(f"/repos/{repo}/pulls/{pr_number}")
        if diff_resp.status_code != 200:
            raise _handle_api_error(diff_resp, f"fetching diff for PR #{pr_number} on '{repo}'")
        return diff_resp.text


async def merge_pr(
    github_token: str,
    repo: str,
    pr_number: int,
    merge_method: str = "squash",
    commit_title: Optional[str] = None,
    commit_message: Optional[str] = None,
) -> Dict[str, Any]:
    """Merges the approved GitHub Pull Request into the target base branch."""
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload: Dict[str, Any] = {"merge_method": merge_method}
    if commit_title:
        payload["commit_title"] = commit_title
    if commit_message:
        payload["commit_message"] = commit_message

    async with httpx.AsyncClient(base_url=GITHUB_API_BASE, headers=headers, timeout=_REQUEST_TIMEOUT) as client:
        resp = await client.put(f"/repos/{repo}/pulls/{pr_number}/merge", json=payload)
        if resp.status_code not in (200, 201):
            raise _handle_api_error(resp, f"merging PR #{pr_number} on '{repo}'")
        data = resp.json()
        return {
            "merged": data.get("merged", True),
            "sha": data.get("sha"),
            "message": data.get("message", "Pull Request successfully merged"),
        }


async def get_workflow_runs(
    github_token: str,
    repo: str,
    branch: str = "main",
) -> List[Dict[str, Any]]:
    """Retrieves recent GitHub Actions workflow runs triggered on the target branch."""
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(base_url=GITHUB_API_BASE, headers=headers, timeout=_REQUEST_TIMEOUT) as client:
        resp = await client.get(f"/repos/{repo}/actions/runs", params={"branch": branch, "per_page": 5})
        if resp.status_code != 200:
            return []
        data = resp.json()
        runs = []
        for r in data.get("workflow_runs", []):
            runs.append({
                "id": r.get("id"),
                "name": r.get("name"),
                "status": r.get("status"),
                "conclusion": r.get("conclusion"),
                "html_url": r.get("html_url"),
                "created_at": r.get("created_at"),
                "event": r.get("event"),
                "head_branch": r.get("head_branch"),
                "head_sha": r.get("head_sha"),
            })
        return runs
