"""Shared Terraform HCL block extraction - balanced-brace matching over the
stack-based files terraform_composer.py produces (foundation.tf/security.tf/
data.tf/application.tf, per tools/graph_builder.py's dependency_graph["stacks"]).

Originally lived only in agents/repair_agent.py (extracting a single failing
block to send to the LLM); moved here so services/github_client.py can reuse
the exact same extraction logic for per-wave PRs (pulling just a wave's
resource blocks out of whichever stack file each lives in) instead of
duplicating brace-matching code a second time.
"""

from typing import Optional

STACK_FILE_NAMES = {"foundation.tf", "security.tf", "data.tf", "application.tf"}


def extract_resource_block(content: str, resource_type: str, resource_name: str) -> Optional[str]:
    """Extract a single `resource "type" "name" { ... }` block via balanced-brace matching."""
    marker = f'resource "{resource_type}" "{resource_name}"'
    start = content.find(marker)
    if start == -1:
        return None
    brace_start = content.find("{", start)
    if brace_start == -1:
        return None
    depth = 0
    for idx in range(brace_start, len(content)):
        if content[idx] == "{":
            depth += 1
        elif content[idx] == "}":
            depth -= 1
            if depth == 0:
                return content[start:idx + 1]
    return None
