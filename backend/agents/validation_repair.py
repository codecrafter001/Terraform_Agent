"""Repair step of the IaC Engineering Agent.

Driven only by `terraform validate`/`init` failures - the adoption code must
describe existing infrastructure exactly, so security findings are reported
(Verification & Risk Agent) and never auto-fixed here.

For each error mapped to a block: ask the LLM to fix that one block, then run
the deterministic invariant checks (tools/hcl_invariants.py) on the whole file
set. A fix that removes resources, silences checks, or smuggles in behavior is
rejected and the block is left as it was.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from services.ollama_client import ollama_client
from services.redis_client import redis_service
from tools.hcl_invariants import check_repair_invariants
from tools.validation_diagnostics import parse_diagnostics

logger = logging.getLogger("terraagent.agents.validation_repair")

PROMPT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", "repair_validation.txt")
MAX_BLOCK_FIXES_PER_CYCLE = int(os.getenv("MAX_LLM_REPAIRS_PER_CYCLE", "3"))


def _find_block(files: Dict[str, str], address: str) -> Tuple[Optional[str], Optional[str]]:
    """(filename, block text) for a resource/data address, via balanced braces."""
    is_data = address.startswith("data.")
    rtype, name = (address[5:] if is_data else address).split(".", 1)
    header = re.compile(rf'^{"data" if is_data else "resource"}\s+"{re.escape(rtype)}"\s+"{re.escape(name)}"',
                        re.MULTILINE)
    for filename, content in files.items():
        match = header.search(content)
        if not match:
            continue
        brace = content.find("{", match.end())
        if brace == -1:
            return None, None
        depth = 0
        for idx in range(brace, len(content)):
            if content[idx] == "{":
                depth += 1
            elif content[idx] == "}":
                depth -= 1
                if depth == 0:
                    return filename, content[match.start(): idx + 1]
        return None, None
    return None, None


def _clean(text: str) -> str:
    text = text.replace("```hcl", "").replace("```terraform", "").replace("```", "").strip()
    # Keep from the first top-level block onwards - models sometimes prepend prose.
    match = re.search(r'^(resource|data)\s+"', text, re.MULTILINE)
    return text[match.start():].strip() if match else text


async def _llm_fix(block: str, error: str) -> Optional[str]:
    try:
        with open(PROMPT_PATH, "r", encoding="utf-8") as f:
            prompt = f.read().format(failing_block=block, error_message=error)
        fixed = await ollama_client.generate(
            prompt,
            system="Output only the corrected Terraform HCL block. No markdown, no prose. Never change values.",
        )
        fixed = _clean(fixed or "")
        return fixed or None
    except Exception as e:  # LLM unavailable - the error just stays unresolved
        logger.warning(f"Validation repair LLM call failed: {e}")
        return None


async def repair_validation_node(state: Dict[str, Any]) -> Dict[str, Any]:
    job_id = state.get("job_id", "")
    cycle = int(state.get("repair_attempts") or 0) + 1
    files: Dict[str, str] = dict(state.get("terraform_files") or {})
    diagnostics = parse_diagnostics(state.get("validation_results") or {}, files)

    fixed: List[str] = []
    rejected: List[Dict[str, Any]] = []
    unresolved: List[str] = []
    seen: set = set()

    for diag in diagnostics:
        address = diag.get("address")
        if not address:
            unresolved.append(diag.get("summary") or "unlocated error")
            continue
        if address in seen:
            continue
        seen.add(address)
        if len(fixed) + len(rejected) >= MAX_BLOCK_FIXES_PER_CYCLE:
            unresolved.append(f"{address}: {diag.get('summary')} (cycle limit reached)")
            continue

        filename, block = _find_block(files, address)
        if not block or not filename:
            unresolved.append(f"{address}: block not found")
            continue

        error_text = f"{diag.get('summary')}\n{diag.get('detail')}".strip()
        candidate = await _llm_fix(block, error_text)
        if not candidate or candidate == block:
            unresolved.append(f"{address}: {diag.get('summary')}")
            continue

        proposal = dict(files)
        proposal[filename] = files[filename].replace(block, candidate, 1)
        violations = check_repair_invariants(files, proposal)
        if violations:
            rejected.append({"address": address, "violations": violations})
            await redis_service.publish_log(
                job_id,
                f"[AGENT:repair_agent] Rejected fix for {address}: {'; '.join(violations)}.",
                agent_name="repair_agent",
            )
            continue
        files = proposal
        fixed.append(address)
        await redis_service.publish_log(
            job_id, f"[AGENT:repair_agent] Fixed {address}: {diag.get('summary')}", agent_name="repair_agent"
        )

    entry = {
        "cycle": cycle,
        "errors": len(diagnostics),
        "fixed": fixed,
        "rejected": rejected,
        "unresolved": unresolved,
    }
    completed = list(state.get("completed_agents", []) or [])
    if "repair_agent" not in completed:
        completed.append("repair_agent")
    return {
        "terraform_files": files,
        "repair_attempts": cycle,
        "repair_history": list(state.get("repair_history") or []) + [entry],
        "completed_agents": completed,
        "current_agent": "validation_agent",
    }
