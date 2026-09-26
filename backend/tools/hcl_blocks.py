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
    """Extract a single `resource "type" "name" { ... }` block via balanced-brace
    matching. Braces inside quoted strings (escaped with \\ as hcl_str writes
    them) and in # comments don't count - discovered tag values can contain
    anything."""
    marker = f'resource "{resource_type}" "{resource_name}"'
    start = content.find(marker)
    while start != -1:
        # The marker must start a line - not appear inside another block's string.
        if start == 0 or content[start - 1] == "\n":
            break
        start = content.find(marker, start + 1)
    if start == -1:
        return None
    brace_start = content.find("{", start + len(marker))
    if brace_start == -1:
        return None
    depth = 0
    in_string = False
    idx = brace_start
    n = len(content)
    while idx < n:
        ch = content[idx]
        if in_string:
            if ch == "\\":
                idx += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "#":
            newline = content.find("\n", idx)
            idx = n if newline == -1 else newline
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return content[start:idx + 1]
        idx += 1
    return None
