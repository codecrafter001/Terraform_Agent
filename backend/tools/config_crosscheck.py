"""Cross-check generated HCL against Terraform's own `plan -generate-config-out`.

Terraform writes config for every import target straight from the live
resource. For each resource we generated, compare every top-level attribute we
set to a literal (string / number / bool) with the value Terraform generated.
A difference is an attribute mistake in our generator - the kind that would
show up as an `update` the moment someone plans the adoption.

References (aws_vpc.x.id, var.*, local.*) and nested blocks are not compared:
Terraform can only generate literals, and nested blocks need schema-aware
matching (later).
"""

import re
from typing import Any, Dict, List, Optional

_HEADER = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', re.MULTILINE)
_ATTR = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")
_LITERAL = re.compile(r'^("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false|null)$')


def _blocks(text: str) -> Dict[str, str]:
    """address -> block body, via balanced braces."""
    out: Dict[str, str] = {}
    for m in _HEADER.finditer(text):
        depth, start = 0, text.index("{", m.start())
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    out[f"{m.group(1)}.{m.group(2)}"] = text[start + 1: i]
                    break
    return out


def top_level_literals(body: str) -> Dict[str, str]:
    """Attributes assigned a literal at the block's top level (depth 0)."""
    attrs: Dict[str, str] = {}
    depth = 0
    for line in body.splitlines():
        stripped = line.split("#", 1)[0].rstrip() if '"' not in line else line.rstrip()
        if depth == 0:
            m = _ATTR.match(stripped)
            if m and _LITERAL.match(m.group(2)):
                attrs[m.group(1)] = m.group(2)
        depth += stripped.count("{") - stripped.count("}")
        depth = max(depth, 0)
    return attrs


def _norm(value: str) -> str:
    return value[1:-1] if value.startswith('"') and value.endswith('"') else value


def compare(our_files: Dict[str, str], generated: str, addresses: Optional[List[str]] = None) -> Dict[str, Any]:
    ours: Dict[str, str] = {}
    for name, content in our_files.items():
        if "/" in name or not name.endswith(".tf"):
            continue  # root module only - that's what Terraform loads
        ours.update(_blocks(content))
    theirs = _blocks(generated)
    targets = addresses if addresses is not None else sorted(ours)

    mismatches: List[Dict[str, str]] = []
    checked = 0
    missing: List[str] = []
    for address in targets:
        if address not in ours:
            continue
        if address not in theirs:
            missing.append(address)
            continue
        checked += 1
        generated_attrs = top_level_literals(theirs[address])
        for key, value in top_level_literals(ours[address]).items():
            if key not in generated_attrs:
                continue  # e.g. write-only/deprecated args Terraform doesn't echo
            if _norm(value) != _norm(generated_attrs[key]):
                mismatches.append({
                    "address": address,
                    "attribute": key,
                    "generated_by_terraagent": _norm(value),
                    "live_per_terraform": _norm(generated_attrs[key]),
                })
    return {
        "resources_checked": checked,
        "resources_missing_from_terraform_output": missing,
        "mismatches": mismatches,
    }
