"""Secret scan for uploaded sources (design doc task 1.4).

A hit blocks the deployment: code that carries credentials must not be
packaged into a Lambda zip or a public S3 bucket. Hits report the file, line
and kind only - never the matched value, so the report itself is safe to
store, log and show.
"""

import os
import re
from typing import List, Optional, TypedDict

from deploy.source_intake import ExtractedSource
from tools.credential_scrubber import AWS_ACCESS_KEY_REGEX, AWS_SECRET_KEY_REGEX

_MAX_SCAN_BYTES = 2 * 1024 * 1024
# Minified bundles have very long lines full of base64-ish text near words like
# "token"; the key=value secret pattern only runs on lines a human wrote.
_MAX_ASSIGNMENT_LINE = 500

_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})\b")),
    ("stripe_live_key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{24,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b")),
]
_BLOCKED_NAMES = [
    ("terraform_state", re.compile(r"\.tfstate(\.backup)?$")),
    ("ssh_private_key", re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)$")),
    ("keystore", re.compile(r"\.(p12|pfx|jks|keystore)$", re.IGNORECASE)),
]


class SecretHit(TypedDict):
    path: str
    line: int  # 0 = the file name itself is the problem
    kind: str


def _aws_key_hit(line: str) -> bool:
    # AWS's own documentation example keys end in EXAMPLE; they're not secrets.
    return any(not m.group(0).endswith("EXAMPLE") for m in AWS_ACCESS_KEY_REGEX.finditer(line))


def scan_source(source: ExtractedSource) -> List[SecretHit]:
    hits: List[SecretHit] = []
    for f in source.files:
        name = f.path.rsplit("/", 1)[-1]
        for name_kind, pattern in _BLOCKED_NAMES:
            if pattern.search(name):
                hits.append({"path": f.path, "line": 0, "kind": name_kind})
                break
        if f.size > _MAX_SCAN_BYTES:
            continue
        with open(os.path.join(source.root, *f.path.split("/")), "rb") as fh:
            raw = fh.read()
        if b"\x00" in raw[:8192]:
            continue  # binary
        text = raw.decode("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            kind: Optional[str] = None
            if _aws_key_hit(line):
                kind = "aws_access_key_id"
            elif len(line) <= _MAX_ASSIGNMENT_LINE and AWS_SECRET_KEY_REGEX.search(line):
                kind = "secret_assignment"
            else:
                kind = next((k for k, p in _PATTERNS if p.search(line)), None)
            if kind:
                hits.append({"path": f.path, "line": lineno, "kind": kind})
                if len(hits) >= 100:
                    return hits
    return hits
