"""Invariant checks run after every repair.

A repair must fix a failing block - never make a problem disappear by deleting
it, silencing a check, or smuggling in behavior. Each check compares the file
set before and after one fix, so anything that already existed before the
repair (e.g. an ignore_changes the composer generated on purpose) doesn't
count against it; only what the repair *introduced* does.
"""

import re
from typing import Dict, List, Set

# A provisioner/command string that would run terraform/tofu apply, destroy or
# import. Content-level tripwire (CLAUDE.md): complements, never replaces,
# terraform_runner.check_argv. Used on generated HCL (validation_agent) and on
# uploaded projects (tools/code_bundle.py).
MUTATING_COMMAND_PATTERN = re.compile(
    r'command\s*=\s*"[^"]*\b(terraform|tofu)\s+(apply|destroy|import)\b', re.IGNORECASE
)

_ADDRESS = re.compile(r'^\s*(resource|data)\s+"([^"]+)"\s+"([^"]+)"', re.MULTILINE)
_MODULE = re.compile(r'^\s*module\s+"([^"]+)"', re.MULTILINE)

# (description, pattern) - counted before vs after; any increase is a violation.
_INTRODUCED_PATTERNS = [
    ("added a lifecycle ignore_changes", re.compile(r"\bignore_changes\b")),
    ("added a scanner suppression comment",
     re.compile(r"(checkov:skip|tfsec:ignore|trivy:ignore|#\s*nosec|kics-scan\s+ignore)", re.IGNORECASE)),
    ("added an external data source", re.compile(r'^\s*data\s+"external"', re.MULTILINE)),
    ("added a provisioner", re.compile(r'^\s*provisioner\s+"', re.MULTILINE)),
    ("added a literal AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("added a literal secret value",
     re.compile(r'\b(password|secret|secret_key|token|private_key|master_password)\s*=\s*"(?!\$\{)[^"]{4,}"',
                re.IGNORECASE)),
]


def addresses(files: Dict[str, str]) -> Set[str]:
    """Every resource/data/module address declared across the file set."""
    found: Set[str] = set()
    for content in files.values():
        for kind, rtype, name in _ADDRESS.findall(content):
            found.add(f"{'data.' if kind == 'data' else ''}{rtype}.{name}")
        for name in _MODULE.findall(content):
            found.add(f"module.{name}")
    return found


def _count(files: Dict[str, str], pattern: "re.Pattern[str]") -> int:
    return sum(len(pattern.findall(content)) for content in files.values())


def check_repair_invariants(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    """Return a human-readable violation for each invariant the change broke
    (empty list = the change is acceptable)."""
    violations: List[str] = []
    removed = sorted(addresses(before) - addresses(after))
    if removed:
        violations.append(f"removed {', '.join(removed[:5])}{'…' if len(removed) > 5 else ''}")
    for description, pattern in _INTRODUCED_PATTERNS:
        if _count(after, pattern) > _count(before, pattern):
            violations.append(description)
    return violations
