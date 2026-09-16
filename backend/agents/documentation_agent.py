"""Documentation Agent: Generates README, migration guides, and packages ZIP."""

import json
import logging
import os
import re
from typing import Any, Dict, List

from services.ollama_client import ollama_client
from services.redis_client import redis_service
from tools.naming import unique_clean_name
from tools.zip_builder import ZipBuilder

logger = logging.getLogger(__name__)

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")


def _load_prompt(filename: str) -> str:
    with open(os.path.join(PROMPTS_DIR, filename), "r", encoding="utf-8") as f:
        return f.read()


# The exact, safe adoption sequence for adopting EXISTING infrastructure. This is
# never left to the LLM: skipping straight to `terraform apply` without first
# running the real `terraform import` commands (see migration/import_plan.md) would
# create duplicate resources instead of adopting the existing ones - a serious
# safety issue observed in testing with LLM-authored versions of this section.
ADOPTION_INSTRUCTIONS = """## Safe Import & Adoption Instructions

Follow these steps in order - do not skip the import step, or Terraform will try
to create duplicate resources instead of adopting your existing ones.

1. Run `terraform init` inside the `terraform/` directory.
2. Run every `terraform import` command listed in `migration/import_plan.md`, in order.
   This links each existing AWS resource to Terraform's state without creating it.
3. Run `terraform plan` and confirm it reports `No changes.` - if it reports any
   changes, review them carefully before proceeding; they mean the generated
   configuration doesn't yet match the real resource exactly.
4. Only once the plan is clean should a human apply it, after review. TerraAgent
   itself never runs `terraform apply` or `terraform destroy`.
"""

# Swapped in for ADOPTION_INSTRUCTIONS instead, whenever a human rejected the
# pending findings via POST /scan/{job_id}/reject - found live: a rejected
# job's README still unconditionally carried the full "Safe Import &
# Adoption Instructions" apply guide, meaning the ZIP looked exactly as
# adoption-ready as an approved one even though a human explicitly said not
# to adopt it. This bundle still exists (for audit/record purposes - what
# was generated, what was found, why it was rejected), but must never read
# as a green light to run any of the commands in migration/import_plan.md.
REJECTED_SECTION = """## Not Approved For Adoption

**A human explicitly REJECTED this configuration - see "Pending Human Approval"
above for the finding(s) and reason. This bundle is kept only as an audit
record of what was generated and why it was not adopted.**

Do NOT run the `terraform import` commands in `migration/import_plan.md`, and
do NOT run `terraform plan`/`apply` against this configuration. If you believe
this was rejected in error, re-run the scan and have a human approve it through
the normal approval flow instead of applying this bundle directly.
"""

# Instructing the LLM not to write this section (via the system prompt below) is
# not sufficient on its own: observed directly in testing, the model sometimes
# ignores that instruction outright and writes the exact forbidden heading anyway,
# with its own version describing "Apply the imported ... files to the AWS
# environment" as a plain numbered step with no plan-review gate - the opposite of
# safe. So this actively strips any such section regardless of whether the model
# obeyed, rather than trusting it did.
_ADOPTION_SECTION_PATTERN = re.compile(
    r"^#{1,3}\s*Safe Import.*?Adoption Instructions.*?(?=^#{1,3}\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL
)


def _strip_llm_adoption_section(text: str) -> str:
    return _ADOPTION_SECTION_PATTERN.sub("", text).strip()


# Same discipline as ADOPTION_INSTRUCTIONS above, applied to a second
# safety-critical section: whether a destructive/behavior_changing finding
# was deliberately left unfixed by repair_agent.py must never depend on the
# LLM actually remembering to write it. Verified live: with 7 required
# sections now (up from the original 5, after this session added Estimated
# Monthly Cost and Pending Human Approval), a real Gemini response
# truncated after the very first section - "Pending Human Approval" simply
# never got written, silently hiding the one piece of information the
# entire risk-tiered-repair feature exists to surface. Stripped and
# unconditionally re-appended here for the same reason ADOPTION_INSTRUCTIONS
# is: never trust the LLM alone for something a human's safety decision
# depends on.
_PENDING_APPROVAL_SECTION_PATTERN = re.compile(
    r"^#{1,3}\s*Pending (Human )?Approval.*?(?=^#{1,3}\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL
)


def _strip_llm_pending_approval_section(text: str) -> str:
    return _PENDING_APPROVAL_SECTION_PATTERN.sub("", text).strip()


def _build_pending_approval_section(pending_approval: Any, repair_risk_tier: Any, approval_decision: Any = None) -> str:
    findings = (pending_approval or {}).get("findings") or []
    if not findings:
        return (
            "## Pending Human Approval\n\n"
            "No findings required escalation to human approval - nothing was left unfixed."
        )
    lines = ["## Pending Human Approval", ""]
    if approval_decision:
        decision = approval_decision.get("decision", "unknown")
        decided_at = approval_decision.get("decided_at", "an unknown time")
        reason = approval_decision.get("reason")
        verb = "APPROVED proceeding despite" if decision == "approved" else "REJECTED this configuration over"
        lines.append(
            f"**A human {verb} {len(findings)} finding(s) below (highest risk tier: "
            f"`{repair_risk_tier}`) on {decided_at}.**" + (f" Note: {reason}" if reason else "")
        )
    else:
        lines.append(
            f"**{len(findings)} finding(s) require manual review before this infrastructure is safe "
            f"to adopt (highest risk tier: `{repair_risk_tier}`). These were deliberately NOT "
            f"auto-repaired - repair_agent only auto-applies `safe_auto`-tier fixes.**"
        )
    lines.append("")
    for f in findings:
        lines.append(
            f"- **[{f.get('tier', 'unknown')}]** `{f.get('resource', 'unknown')}` "
            f"({f.get('tool', 'unknown')} {f.get('rule_id', '')}, {f.get('severity', 'unknown')}): "
            f"{f.get('description', '')}"
        )
    return "\n".join(lines)


def _build_drift_report(drift_results: Any) -> str:
    """Standalone doc (migration/import_plan.md's sibling), not injected into
    README.md - documentation_agent produces it, github_client.py's PR body
    pulls its own summary from the same state["drift_results"] independently,
    same pattern as plan_equivalence_results/security_results already use."""
    drift_results = drift_results or {}
    if drift_results.get("skipped"):
        return (
            "# TerraAgent Drift Reconciliation Report\n\n"
            f"Not run: {drift_results.get('reason', 'skipped')}.\n"
        )

    findings = drift_results.get("findings") or []
    lines = [
        "# TerraAgent Drift Reconciliation Report",
        "",
        "Diffs each adopted resource's live AWS attributes against what was actually written into "
        "the generated Terraform - see `reports/drift_results.json` for the raw data.",
        "",
        f"- **Resources checked against live AWS:** {drift_results.get('resources_checked', 0)}",
        f"- **No longer exist in AWS:** {drift_results.get('resources_missing', 0)}",
        f"- **Destructive-equivalent finding(s):** {drift_results.get('destructive_equivalent_count', 0)}",
        f"- **Behavior-changing finding(s):** {drift_results.get('behavior_changing_count', 0)}",
        f"- **Informational (tag) difference(s):** {drift_results.get('informational_count', 0)}",
        "",
    ]
    if not findings:
        lines.append("No drift detected - every checked resource's live attributes match the generated configuration.")
        return "\n".join(lines)

    lines.append(
        "Destructive-equivalent and behavior-changing findings below were escalated to "
        "\"Pending Human Approval\" in `README.md` and never auto-repaired - this agent only "
        "reports drift, it never modifies generated HCL."
    )
    lines.append("")
    for f in findings:
        lines.append(f"## `{f.get('resource', 'unknown')}` - {f.get('attribute', 'unknown')}")
        lines.append(f"- **Tier:** {f.get('tier', 'unknown')}")
        lines.append(f"- **Generated:** `{f.get('generated_value')}`")
        lines.append(f"- **Live:** `{f.get('live_value')}`")
        lines.append(f"- {f.get('description', '')}")
        lines.append("")
    return "\n".join(lines)


async def _generate_readme(
    region: str, resources: List[Dict[str, Any]], validation_passed: bool, risk_score: int,
    fallback: str, tf_filenames: List[str], scanners_skipped: List[str], cost_summary: str,
    pending_approval_section: str, adoption_section: str
) -> str:
    try:
        template = _load_prompt("documentation.txt")
        prompt = template.format(
            region=region,
            resource_count=len(resources),
            resources_summary=json.dumps(
                [{"type": r.get("resource_type"), "id": r.get("id"), "name": r.get("name")} for r in resources]
            ),
            validation_status="PASSED" if validation_passed else "FAILED",
            security_score=risk_score,
            scanners_skipped=", ".join(sorted(scanners_skipped)) if scanners_skipped else "none - all scanners ran",
            cost_summary=cost_summary
        )
        # terraform_composer.py now splits resource output across stack-based files
        # (foundation.tf/security.tf/data.tf/application.tf) rather than one fixed
        # resources.tf, so the actual file set varies per scan - build the whitelist
        # from what this run really produced instead of a static list.
        tf_file_list = ", ".join(sorted(tf_filenames))
        readme = await ollama_client.generate(
            prompt,
            system=(
                "Output clean Markdown only. Do NOT write a 'Safe Import & Adoption "
                "Instructions' section, nor a 'Pending Human Approval' section (or any "
                "section describing how to run terraform import/plan/apply, or whether "
                "repairs need human review) - both are provided separately and must not "
                "be duplicated or contradicted. Cover only: Infrastructure Overview, "
                "Discovered Architecture Summary, Generated Terraform Files Structure, "
                "Security & Validation Compliance Summary, Estimated Monthly Cost, and "
                "Assumptions and Inferred Defaults. For the Generated Terraform Files "
                "Structure section, list ONLY these real files - never invent others "
                "such as terraform.tfstate or "
                f"terraform.tfvars, which this tool never creates: {tf_file_list} (all in "
                "terraform/), inventory.json, inventory.csv, dependency_graph.json, "
                "dependency_graph.html, reports/validation_report.json, "
                "reports/security_report.json, reports/cost_report.json, "
                "reports/pending_approval.json, reports/drift_results.json, drift_report.md, "
                "migration/import_plan.md, migration/migration_checklist.md, assumptions.md. For Estimated Monthly "
                "Cost, only report the figure given in Input Details - never estimate or "
                "invent a cost yourself."
            ),
            # Bumped from 800: with 6 required sections (was 5 before this session
            # added Estimated Monthly Cost), 800 tokens was already tight - verified
            # live that a real Gemini response truncated after the very first
            # section, silently dropping everything after it. Still no substitute
            # for the fix below (never trusting the LLM for the pending-approval
            # section at all) - this just reduces how often OTHER sections
            # (architecture summary, files structure, cost, assumptions) get cut off.
            num_predict=1400
        )
        readme = readme.replace("```markdown", "").replace("```", "").strip()
        readme = _strip_llm_adoption_section(readme)
        readme = _strip_llm_pending_approval_section(readme)
        if readme and readme.startswith("#"):
            return f"{readme}\n\n{pending_approval_section}\n\n{adoption_section}"
    except Exception as e:
        logger.warning(f"LLM README generation failed, using deterministic fallback: {e}")
    return fallback


def _import_cmd(res: Dict[str, Any]) -> str:
    r_type = res.get("resource_type")
    r_id = res.get("id")
    clean_name = unique_clean_name(res.get("name", r_id), r_id)
    return f"terraform import {r_type}.{clean_name} {r_id}"


async def documentation_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "unknown")
    region = state.get("region", "us-east-1")
    resources = state.get("resources", [])
    tf_files = state.get("terraform_files", {})
    dep_graph = state.get("dependency_graph", {})
    val_res = state.get("validation_results", {})
    sec_res = state.get("security_results", {})
    cost_res = state.get("cost_results", {}) or {}
    drift_res = state.get("drift_results", {}) or {}
    adoption_plan = state.get("adoption_plan", {}) or {}
    pending_approval = state.get("pending_approval")

    await redis_service.publish_log(
        job_id,
        "[AGENT:documentation_agent] Generating comprehensive README, assumptions log, and packaging final ZIP...",
        agent_name="documentation_agent"
    )

    # 1. Build import commands - classification-aware. "skip" (e.g. AWS
    # service-linked roles) and "data_source" (e.g. the default security
    # group) resources get NO resource block from terraform_composer.py
    # (only "data_source" gets a `data` block, "skip" gets nothing at all),
    # so a `terraform import` command for either always fails outright -
    # "resource address does not exist in the configuration". Every other
    # recommended_action ("import", "manual_review") does get a real
    # `resource` block, so importing them is valid, even if manual_review
    # ones need a second look first.
    classification_map = {
        c["resource_id"]: c["recommended_action"]
        for c in (state.get("classification_results") or {}).get("classifications", [])
    }
    resources_by_id = {r["id"]: r for r in resources if r.get("id")}
    importable_ids = {
        rid for rid in resources_by_id
        if classification_map.get(rid, "import") not in ("skip", "data_source")
    }

    waves = adoption_plan.get("waves") or []
    manual_review_ids = [rid for rid in importable_ids if classification_map.get(rid) == "manual_review"]

    if waves:
        # Group by the Adoption Planning Agent's dependency-safe, risk-scored
        # waves instead of raw discovery order - a wave's resources have no
        # unresolved dependency on anything in a later wave.
        sections = []
        for wave in waves:
            wave_ids = [rid for rid in wave.get("resource_ids", []) if rid in resources_by_id]
            if not wave_ids:
                continue
            header = f"# Wave {wave.get('wave')} - risk: {wave.get('risk_level', 'unknown')}"
            for signal in wave.get("risk_signals", []):
                header += f"\n#   - {signal}"
            cmds = "\n".join(_import_cmd(resources_by_id[rid]) for rid in wave_ids)
            sections.append(f"{header}\n{cmds}")
        waves_block = "\n\n".join(sections)
    else:
        # adoption_plan/waves unavailable (e.g. a partial/manual invocation
        # that skipped adoption_planning_agent) - fall back to discovery
        # order, still classification-filtered so #1 above always holds.
        fallback_ids = [rid for rid in importable_ids if rid not in manual_review_ids]
        waves_block = "\n".join(_import_cmd(resources_by_id[rid]) for rid in fallback_ids)

    manual_review_block = ""
    if manual_review_ids:
        cmds = "\n".join(_import_cmd(resources_by_id[rid]) for rid in manual_review_ids)
        manual_review_block = f"""

## Manual Review Required Before Importing

These resources have a real Terraform configuration generated for them, but were
flagged for manual review (orphaned, unsupported, or otherwise ambiguous) - inspect
each one before running its import command; do not run these blindly alongside the
waves above.

```bash
{cmds}
```
"""

    import_plan_md = f"""# TerraAgent Import Plan

Verified, non-destructive `terraform import` commands, grouped into dependency-safe
migration waves by the Adoption Planning Agent. Run these from inside the `terraform/`
directory, after `terraform init`, completing each wave before starting the next.
See `migration_checklist.md` for the full adoption procedure. Resources classified
"skip" (AWS-managed) or "use_data_source" (referenced, not owned) are intentionally
excluded - there's no `resource` block for either to import into.

```bash
{waves_block}
```
{manual_review_block}"""

    migration_checklist = """# TerraAgent Infrastructure Migration & Import Guide

This document contains the step-by-step, non-destructive procedure to adopt existing AWS resources into Terraform state.

## Step 1: Initialize Terraform
```bash
terraform init
```

## Step 2: Safe Resource Adoption (terraform import)
Run every command listed in `import_plan.md`, in order, to link your existing live AWS resources to Terraform state without creating duplicates.

## Step 3: Verify Plan
```bash
terraform plan
```
Verify that Terraform reports: `No changes. Your infrastructure matches the configuration.`
"""

    scanners_skipped = sec_res.get("scanners_skipped") or []
    security_score_line = f"- **Security Risk Score**: {sec_res.get('risk_score', 0)} / 100"
    if scanners_skipped:
        # A risk score computed from fewer scanners than the pipeline normally runs
        # is not the same claim as "0/100, verified clean" - say so explicitly
        # rather than let a silent tool-skip read as a clean bill of health.
        security_score_line += (
            f" (WARNING: {', '.join(sorted(scanners_skipped))} could not run - "
            "not installed in this environment - and are NOT reflected in this score)"
        )

    # Same honesty discipline as scanners_skipped above - tool_skipped means
    # Infracost never ran (missing binary or INFRACOST_API_KEY), which must
    # never be presented as "estimated cost: $0.00".
    if cost_res.get("tool_skipped"):
        cost_summary = "not estimated - Infracost was unavailable for this run"
        cost_line = "- **Estimated Monthly Cost**: not estimated (Infracost unavailable - missing binary or API key)"
    else:
        total_cost = cost_res.get("total_monthly_cost", 0.0)
        currency = cost_res.get("currency", "USD")
        cost_summary = f"${total_cost:.2f} {currency}/month across {len(cost_res.get('resources', []))} priced resource(s)"
        cost_line = f"- **Estimated Monthly Cost**: ${total_cost:.2f} {currency}/month"
        unsupported = cost_res.get("unsupported_resource_count", 0)
        if unsupported:
            cost_line += f" ({unsupported} resource(s) not supported by Infracost, excluded from this total)"

    # repair_agent.py deliberately leaves behavior_changing/destructive
    # findings unfixed rather than risk an incorrect automated change - a job
    # can complete "successfully" while still carrying findings nobody has
    # actually looked at, so this must be visible, not buried in a JSON file
    # nobody opens. Built once here and reused unmodified in both the
    # fallback and the LLM-generated path (_generate_readme strips anything
    # the LLM wrote under this heading and re-appends this exact text) - see
    # _build_pending_approval_section's docstring for why this can never be
    # left to the LLM alone.
    approval_decision = state.get("approval_decision")
    pending_approval_section = _build_pending_approval_section(
        pending_approval, state.get("repair_risk_tier"), approval_decision
    )
    approval_line = (
        f"\n- **⚠ Human Approval Required**: {len(pending_approval.get('findings', []))} finding(s) - "
        f"see 'Pending Human Approval' below." if pending_approval else ""
    )
    # Never the LLM's decision to make, same discipline as ADOPTION_INSTRUCTIONS
    # itself - a rejected job must never carry the standard apply guide, or the
    # README (and the ZIP it's bundled into) reads exactly as adoption-ready as
    # an approved one despite a human explicitly saying not to.
    is_rejected = bool(approval_decision) and approval_decision.get("decision") == "rejected"
    adoption_section = REJECTED_SECTION if is_rejected else ADOPTION_INSTRUCTIONS

    readme_fallback = f"""# TerraAgent Generated Infrastructure Bundle

- **Generated For Job**: `{job_id}`
- **AWS Region**: `{region}`
- **Discovered Resources**: {len(resources)}
- **Validation Status**: {'PASSED' if val_res.get('passed') else 'COMPLETED'}
{security_score_line}
{cost_line}{approval_line}

---

## File Structure
- `terraform/`: Modular Terraform configuration (`{'`, `'.join(sorted(tf_files.keys()))}`).
- `inventory.json` / `inventory.csv`: Complete raw metadata of all discovered cloud assets.
- `dependency_graph.json`: Full topological relationship DAG.
- `dependency_graph.html`: Standalone interactive D3 visualization - open directly in a browser, no server needed.
- `reports/`: Granular validation, static security analysis, Infracost cost, and pending-approval reports.
- `migration/import_plan.md`: The raw, ordered `terraform import` commands.
- `migration/migration_checklist.md`: Step-by-step non-destructive resource adoption procedure.

{pending_approval_section}

{adoption_section}"""

    readme_md = await _generate_readme(
        region, resources, bool(val_res.get("passed")), sec_res.get("risk_score", 0),
        readme_fallback, list(tf_files.keys()), scanners_skipped, cost_summary,
        pending_approval_section, adoption_section
    )

    assumptions_md = """# TerraAgent Synthesis Assumptions

1. **Provider Version**: HashiCorp AWS provider `~> 5.0` assumed for modern syntax support.
2. **State Management**: Local state initialized. Remote S3 backend recommended prior to team deployment.
3. **Security Standards**: Public accessibility blocked by default where applicable.
"""

    docs = {
        "README.md": readme_md,
        "migration/import_plan.md": import_plan_md,
        "migration/migration_checklist.md": migration_checklist,
        "drift_report.md": _build_drift_report(drift_res),
        "assumptions.md": assumptions_md
    }

    # Package output ZIP (AES-256 encrypted if the caller supplied a password)
    output_dir = os.getenv("OUTPUT_DIR", "/tmp/terraagent")
    zip_info = ZipBuilder.build_zip(
        job_id=job_id,
        tf_files=tf_files,
        inventory={"resources": resources, "region": region},
        graph_data=dep_graph,
        validation_results=val_res,
        security_results=sec_res,
        cost_results=cost_res,
        pending_approval=pending_approval,
        drift_results=drift_res,
        generation_manifest=state.get("generation_manifest"),
        docs=docs,
        output_dir=output_dir,
        password=state.get("zip_password")
    )

    await redis_service.publish_log(
        job_id,
        f"[AGENT:documentation_agent] Output ZIP bundle created successfully at {zip_info['zip_path']}.",
        agent_name="documentation_agent"
    )

    completed_agents = list(state.get("completed_agents", []))
    if "documentation_agent" not in completed_agents:
        completed_agents.append("documentation_agent")

    return {
        "documentation": docs,
        "zip_path": zip_info["zip_path"],
        "zip_sha256": zip_info["sha256"],
        "zip_manifest": zip_info["manifest"],
        "completed_agents": completed_agents,
        "current_agent": "complete",
        "progress_percentage": 100,
        "status": "COMPLETE"
    }

