"""Turn `terraform validate` / `init` failures into repairable targets.

Each diagnostic is mapped to the file, line and - where possible - the
enclosing resource/data block, so repair can fix exactly that block and
nothing else.
"""

import json
import re
from typing import Any, Dict, List, Optional

_TOP_LEVEL = re.compile(r'^(resource|data)\s+"([^"]+)"\s+"([^"]+)"')
_TEXT_LOCATION = re.compile(r"on\s+(\S+\.tf)\s+line\s+(\d+)")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def enclosing_block(content: str, line: int) -> Optional[str]:
    """Address (type.name / data.type.name) of the top-level resource or data
    block that contains 1-based `line`, or None."""
    current: Optional[str] = None
    depth = 0
    for idx, text in enumerate(content.splitlines(), start=1):
        if depth == 0:
            match = _TOP_LEVEL.match(text)
            current = None
            if match:
                kind, rtype, name = match.groups()
                current = f"{'data.' if kind == 'data' else ''}{rtype}.{name}"
        if idx == line:
            return current
        depth = max(0, depth + text.count("{") - text.count("}"))
    return None


def _diag(summary: str, detail: str, filename: Optional[str], line: Optional[int],
          files: Dict[str, str]) -> Dict[str, Any]:
    address = None
    if filename and line:
        content = files.get(filename)
        if content is None:  # validate reports paths relative to the root module
            content = next((c for f, c in files.items() if f.endswith(filename)), None)
        if content is not None:
            address = enclosing_block(content, line)
    return {"summary": summary.strip(), "detail": detail.strip(), "filename": filename, "line": line,
            "address": address}


def parse_diagnostics(validation_results: Dict[str, Any], files: Dict[str, str]) -> List[Dict[str, Any]]:
    """Error diagnostics from the validation checks (JSON from `validate -json`
    when available, otherwise the "Error: ... on file.tf line N" text form)."""
    out: List[Dict[str, Any]] = []
    for check in validation_results.get("checks", []) or []:
        if check.get("passed") or check.get("check_name") not in ("validate", "init"):
            continue
        raw = _ANSI.sub("", check.get("output") or "")
        parsed = False
        start = raw.find("{")
        if start != -1:
            try:
                data = json.loads(raw[start: raw.rfind("}") + 1])
                for d in data.get("diagnostics", []) or []:
                    if d.get("severity") != "error":
                        continue
                    rng = d.get("range") or {}
                    out.append(_diag(d.get("summary", ""), d.get("detail", ""), rng.get("filename"),
                                     (rng.get("start") or {}).get("line"), files))
                parsed = True
            except (ValueError, AttributeError):
                parsed = False
        if not parsed:
            for chunk in raw.split("Error:")[1:]:
                summary = chunk.strip().splitlines()[0] if chunk.strip() else "error"
                loc = _TEXT_LOCATION.search(chunk)
                out.append(_diag(summary, chunk[:600], loc.group(1) if loc else None,
                                 int(loc.group(2)) if loc else None, files))
    return out
