"""Repair Agent: Fixes validation and policy failures via targeted HCL patches.

Two remediation paths run each cycle:
1. A deterministic patch for the one well-understood, always-safe case (missing
   S3 default encryption) - no LLM involved, zero risk of hallucination.
2. For CRITICAL/HIGH findings the deterministic patch doesn't cover, the specific
   failing resource block is extracted and sent to the local Ollama LLM (with the
   scanner's own error/description text) for a targeted rewrite. Only tfsec and
   checkov findings carry a real Terraform resource address (trivy's "resource"
   field is an OPA query path, not an address, so it can't be block-matched).
"""

import logging
import os
import re
from typing import Any, Dict, List, Literal, Optional

from services.ollama_client import ollama_client
from services.redis_client import redis_service
from tools.hcl_blocks import STACK_FILE_NAMES, extract_resource_block as _extract_block

RepairTier = Literal["safe_auto", "behavior_changing", "destructive"]
_TIER_RANK: Dict[str, int] = {"safe_auto": 0, "behavior_changing": 1, "destructive": 2}

logger = logging.getLogger(__name__)

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")
# Each LLM repair call can take minutes on CPU-only inference; kept small so a repair
# cycle has a bounded worst-case latency (this multiplies with OLLAMA_TIMEOUT_SECONDS
# and the pipeline's own 2-repair-cycle cap).
MAX_LLM_REPAIRS_PER_CYCLE = int(os.getenv("MAX_LLM_REPAIRS_PER_CYCLE", "2"))


def _load_prompt(filename: str) -> str:
    with open(os.path.join(PROMPTS_DIR, filename), "r", encoding="utf-8") as f:
        return f.read()


_VALID_TOP_LEVEL_STARTS = (
    'resource "', 'output "', 'data "', 'variable "', 'locals ', 'module "', 'provider "', 'terraform '
)


def _clean_llm_hcl(text: str) -> str:
    """Strip prose the LLM adds despite instructions not to. Walks top-level segments
    one at a time; a segment is only kept if it actually starts with a real HCL
    top-level keyword - a bare, unwrapped block (e.g. the LLM emitting a nested
    `ingress { ... }` at the top level instead of inside a resource block) or any
    other prose immediately stops consumption, discarding everything from there on."""
    start = text.find('resource "')
    if start == -1:
        return ""
    text = text[start:]

    pos = 0
    last_good_end = 0
    n = len(text)
    while pos < n:
        while pos < n and (text[pos].isspace() or text[pos] == "#"):
            if text[pos] == "#":
                nl = text.find("\n", pos)
                pos = nl + 1 if nl != -1 else n
            else:
                pos += 1
        if pos >= n or not text[pos:].startswith(_VALID_TOP_LEVEL_STARTS):
            break
        brace = text.find("{", pos)
        if brace == -1:
            break
        depth = 0
        i = brace
        while i < n:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0:
            break
        last_good_end = i + 1
        pos = last_good_end

    return text[:last_good_end].strip() if last_good_end else ""


async def _repair_block_via_llm(block: str, error_message: str) -> Optional[str]:
    try:
        template = _load_prompt("repair_fix.txt")
        prompt = template.format(failing_block=block, error_message=error_message)
        fixed = await ollama_client.generate(
            prompt,
            system=(
                "Output only the corrected Terraform HCL block. No markdown fences, no backtick "
                "characters anywhere, no prose. Keep the exact same resource type and name as the "
                "input block. Only reference attributes that are real, documented arguments of this "
                "AWS provider resource type; never invent attribute names."
            )
        )
        fixed = fixed.replace("```hcl", "").replace("```", "").replace("`", "").strip()
        fixed = _clean_llm_hcl(fixed)
        if fixed:
            return fixed
    except Exception as e:
        logger.warning(f"LLM repair call failed: {e}")
    return None


# terraform_composer.py now splits resource output across stack-based files instead
# of one flat resources.tf (foundation/security/data/application, per
# tools/graph_builder.py's dependency_graph["stacks"]). Any of these may or may not
# be present in a given run's tf_files, so both patches below loop across whichever
# of these keys actually exist rather than assuming a single fixed filename.
# STACK_FILE_NAMES / _extract_block now live in tools/hcl_blocks.py, shared with
# services/github_client.py's per-wave PR extraction.

# Resource types the composer generates through its proven-correct deterministic
# templates. These blocks are already structurally sound; an LLM "fix" applied to
# one risks corrupting good HCL with a hallucinated block type (observed in testing:
# the LLM invented an S3-only `public_access_block` nested inside a security group).
# LLM-driven repair is reserved for resource types that were LLM-generated to begin
# with, where there is nothing already-correct to lose.
DETERMINISTIC_RESOURCE_TYPES = {
    "aws_vpc", "aws_subnet", "aws_security_group", "aws_instance",
    "aws_s3_bucket", "aws_s3_bucket_server_side_encryption_configuration",
    "aws_s3_bucket_public_access_block", "aws_route_table", "aws_route_table_association",
}


def _actionable_findings(security_results: Dict[str, Any]) -> List[Dict[str, Any]]:
    """CRITICAL/HIGH findings against non-deterministic resource types, from tools
    whose 'resource' field is a real Terraform address."""
    findings = security_results.get("findings", []) or []
    actionable = []
    for f in findings:
        if f.get("severity") not in ("CRITICAL", "HIGH"):
            continue
        if f.get("tool") not in ("tfsec", "checkov"):
            continue
        address = f.get("resource") or ""
        resource_type = re.sub(r"\[.*\]$", "", address).split(".", 1)[0]
        if not resource_type or resource_type in DETERMINISTIC_RESOURCE_TYPES:
            continue
        actionable.append(f)
    return actionable


# Deterministic keyword heuristic over rule_id/description - no real
# `terraform plan` diff exists in this codebase (that's the separate,
# unbuilt Plan-Equivalence Agent), so this cannot know for certain whether a
# given attribute change would actually force AWS to replace the resource.
# Deliberately conservative: false-conservative (escalating something that
# was actually safe) is the acceptable failure direction; false-permissive
# (auto-applying something that was actually destructive) never is.
_DESTRUCTIVE_KEYWORDS = (
    "storage_encrypted", "engine_version", "allocated_storage", "identifier",
    "availability_zone", "instance_class", "storage_type", "master_username",
    "name_prefix",
)
# Encryption-at-rest on a managed database engine is essentially always a
# create-time-only attribute across AWS's managed DB services - it cannot be
# toggled on an existing instance without a snapshot-and-restore into a brand
# new one, which Terraform models as a full replace. Checked ahead of the
# generic "encrypt" keyword below (which exists for cases like S3 bucket
# encryption, safely added in-place via a separate resource - see
# terraform_composer.py's deterministic S3 template), or that generic keyword
# would just as happily match "storage encryption enabled"/"encrypted at
# rest" and misclassify the single most common real destructive RDS finding
# as merely behavior_changing. Verified live against genuine tfsec/checkov
# output: CKV_AWS_16 ("data stored in the RDS is securely encrypted at
# rest") and AVD-AWS-0080 ("does not have storage encryption enabled") both
# fell through to behavior_changing before this fix - neither description
# contains the literal string "storage_encrypted".
_DESTRUCTIVE_DB_RESOURCE_TYPES = {
    "aws_db_instance", "aws_rds_cluster", "aws_rds_cluster_instance",
    "aws_elasticache_cluster", "aws_elasticache_replication_group",
    "aws_docdb_cluster", "aws_neptune_cluster",
}
_BEHAVIOR_CHANGING_KEYWORDS = (
    "encrypt", "public", "acl", "ingress", "egress", "cidr", "policy",
    "versioning", "logging", "access_block", "port", "backup",
)
# Findings that are purely structural/administrative - they don't change
# what the resource actually does at runtime, only its metadata or how the
# HCL is written. This is the only category the LLM auto-fix path is
# actually allowed to touch (see repair_agent_node) - everything else is
# either destructive or behavior_changing and always escalates.
_SAFE_AUTO_KEYWORDS = (
    "tag", "tagging", "description", "naming", "deprecated argument",
    "deprecated attribute",
)


def _classify_repair_tier(finding: Dict[str, Any]) -> RepairTier:
    text = f"{finding.get('rule_id', '')} {finding.get('description', '')}".lower()
    resource_type = re.sub(r"\[.*\]$", "", finding.get("resource") or "").split(".", 1)[0]

    if resource_type in _DESTRUCTIVE_DB_RESOURCE_TYPES and "encrypt" in text:
        return "destructive"
    if any(kw in text for kw in _DESTRUCTIVE_KEYWORDS):
        return "destructive"
    if any(kw in text for kw in _BEHAVIOR_CHANGING_KEYWORDS):
        return "behavior_changing"
    if any(kw in text for kw in _SAFE_AUTO_KEYWORDS):
        return "safe_auto"
    # Unknown finding shape - escalate rather than silently treat as safe.
    return "behavior_changing"


async def repair_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "unknown")
    attempts = state.get("repair_attempts", 0) + 1
    tf_files = state.get("terraform_files", {})
    security_results = state.get("security_results", {})

    await redis_service.publish_log(
        job_id,
        f"[AGENT:repair_agent] Initiating repair cycle #{attempts} to resolve detected issues...",
        agent_name="repair_agent"
    )

    resource_file_names = [f for f in tf_files if f in STACK_FILE_NAMES]

    # 1. Deterministic patch: ensure every S3 bucket has default encryption. A given
    # bucket only ever lives in one stack file, so this is a plain per-file loop,
    # not a cross-file search.
    for filename in resource_file_names:
        content = tf_files[filename]
        bucket_names = re.findall(r'resource\s+"aws_s3_bucket"\s+"([A-Za-z0-9_]+)"', content)
        for bucket_name in bucket_names:
            if f'"{bucket_name}_encryption"' not in content:
                content += f"""

# Remediated by TerraAgent Repair Agent: Enforce S3 Default Encryption
resource "aws_s3_bucket_server_side_encryption_configuration" "{bucket_name}_encryption" {{
  bucket = aws_s3_bucket.{bucket_name}.id

  rule {{
    apply_server_side_encryption_by_default {{
      sse_algorithm = "AES256"
    }}
  }}
}}
"""
        tf_files[filename] = content

    # 2. LLM-driven patch, but ONLY for findings classified safe_auto. Anything
    # behavior_changing or destructive is never sent to the LLM and never
    # auto-applied - it's collected into pending_approval instead, for a
    # human to review. The target resource could be in any stack file, so
    # search each in turn and stop at the first match - a resource address
    # only ever exists in one file.
    llm_repairs = 0
    escalated: List[Dict[str, Any]] = []
    cycle_max_tier: RepairTier = "safe_auto"

    for finding in _actionable_findings(security_results):
        tier = _classify_repair_tier(finding)
        if tier != "safe_auto":
            escalated.append({**finding, "tier": tier})
            if _TIER_RANK[tier] > _TIER_RANK[cycle_max_tier]:
                cycle_max_tier = tier
            continue

        if llm_repairs >= MAX_LLM_REPAIRS_PER_CYCLE:
            continue
        address = re.sub(r"\[.*\]$", "", finding["resource"])  # strip count/for_each index
        if "." not in address:
            continue
        resource_type, resource_name = address.split(".", 1)

        filename, block = None, None
        for fn in resource_file_names:
            block = _extract_block(tf_files[fn], resource_type, resource_name)
            if block:
                filename = fn
                break
        if not block:
            continue

        fixed_block = await _repair_block_via_llm(block, finding.get("description", ""))
        if fixed_block and fixed_block != block:
            tf_files[filename] = tf_files[filename].replace(block, fixed_block, 1)
            llm_repairs += 1

    # plan_equivalence_agent (which now runs before policy_agent/repair_agent
    # in the pipeline) may already have populated pending_approval this same
    # run - merge into it rather than overwrite, or a repair-cycle escalation
    # would silently discard an earlier plan-diff finding nobody has reviewed.
    existing_pending = state.get("pending_approval")
    existing_findings = (existing_pending or {}).get("findings", [])
    existing_tier = state.get("repair_risk_tier")

    if escalated:
        pending_approval = {"reason": "repair_requires_human_approval", "findings": existing_findings + escalated}
        repair_risk_tier = cycle_max_tier
        if existing_tier and _TIER_RANK.get(existing_tier, -1) > _TIER_RANK[repair_risk_tier]:
            repair_risk_tier = existing_tier
    else:
        pending_approval = existing_pending
        repair_risk_tier = existing_tier

    if escalated:
        await redis_service.publish_log(
            job_id,
            f"[AGENT:repair_agent] {len(escalated)} finding(s) require human approval "
            f"(highest tier: {cycle_max_tier}) - not auto-repaired.",
            agent_name="repair_agent"
        )

    # Mirrors graph.py::repair_or_done's own routing decision - repair_agent
    # already knows whether anything escalated, so the forward-looking
    # current_agent hint can be accurate instead of a static guess. Without
    # this, current_agent stayed at whatever policy_agent had already set
    # ("cost_agent") for the entire repair cycle, showing the pipeline as
    # two steps further along than it actually was while repair/validation
    # were still running - repair_agent was the one node with no hint at
    # all, alongside terraform_composer (see that file's fix).
    #
    # A pending_approval now means the graph routes straight to END (see
    # repair_or_done) rather than continuing to cost_agent - "awaiting_approval"
    # is a pseudo-node the frontend recognizes as a genuine halt, not further
    # pipeline movement.
    next_agent = "awaiting_approval" if pending_approval else "validation_agent"

    await redis_service.publish_log(
        job_id,
        f"[AGENT:repair_agent] Repair cycle #{attempts} completed "
        f"({llm_repairs} LLM-driven fix(es) applied). "
        + ("Escalated findings pending approval - skipping re-validation." if pending_approval else "Re-running validation."),
        agent_name="repair_agent"
    )

    completed_agents = list(state.get("completed_agents", []))
    if "repair_agent" not in completed_agents:
        completed_agents.append("repair_agent")

    result: Dict[str, Any] = {
        "repair_attempts": attempts,
        "terraform_files": tf_files,
        "completed_agents": completed_agents,
        "repair_risk_tier": repair_risk_tier,
        "pending_approval": pending_approval,
        "current_agent": next_agent
    }
    if pending_approval:
        result["status"] = "AWAITING_APPROVAL"
    return result
