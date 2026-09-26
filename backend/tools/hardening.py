"""The optional Hardening proposal - security fixes kept out of the adoption code.

The adoption code must plan with zero changes, so security findings are only
reported there. This module turns the well-understood ones into concrete,
explained Terraform changes on top of the adoption code, for a separate
Hardening PR a human chooses to merge. Deterministic only (no LLM), and only
where discovery shows the setting is actually missing:

- S3 public access block, when a bucket has none or a partial one
  (behavior-changing: intentionally public buckets stop being public).
- S3 default encryption, when a bucket has no default encryption rule.
- EC2 IMDSv2 (http_tokens = "required"), when an instance still allows IMDSv1
  (behavior-changing: software still using IMDSv1 loses instance credentials).

Every other HIGH/CRITICAL finding becomes a manual recommendation with the
scanner's own text. Nothing here ever touches AWS or runs terraform; the
Hardening agent validates the result (agents/hardening_agent.py).
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from tools.hcl_blocks import STACK_FILE_NAMES, extract_resource_block
from tools.naming import unique_clean_name

PAB_FIELDS = ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets")

# Which scanner findings a change resolves - matched on rule text, never executed.
_FINDING_PATTERNS = {
    "s3_public_access_block": re.compile(r"public.?access|block.?public|public.?acl|public.?polic|restrict.?public", re.I),
    "s3_encryption": re.compile(r"encrypt|sse|kms", re.I),
    "ec2_imdsv2": re.compile(r"imds|metadata|http.?tokens", re.I),
}


def _address(res: Dict[str, Any]) -> Tuple[str, str]:
    name = unique_clean_name(res.get("name", res.get("id")), res.get("id"))
    return f"{res.get('resource_type')}.{name}", name


def _file_with(files: Dict[str, str], rtype: str, name: str) -> Optional[str]:
    for filename in sorted(f for f in files if f in STACK_FILE_NAMES):
        if extract_resource_block(files[filename], rtype, name):
            return filename
    return None


def _related(findings: List[Dict[str, Any]], address: str, kind: str) -> List[Dict[str, Any]]:
    pattern = _FINDING_PATTERNS[kind]
    return [
        f for f in findings
        if str(f.get("resource") or "").split("[")[0] == address
        and pattern.search(f"{f.get('rule_id', '')} {f.get('description', '')}")
    ]


def propose(
    terraform_files: Dict[str, str],
    resources: List[Dict[str, Any]],
    managed_ids: List[str],
    security_results: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the proposal. Returns {files (only the changed ones, full
    content), changes, recommendations}. files is empty when nothing applies."""
    files = dict(terraform_files)
    findings = list((security_results or {}).get("findings", []) or [])
    changes: List[Dict[str, Any]] = []
    addressed: set = set()
    managed = set(managed_ids)

    for res in resources:
        if res.get("id") not in managed:
            continue
        address, name = _address(res)
        rtype = res.get("resource_type")
        filename = _file_with(files, rtype, name)
        if not filename:
            continue

        if rtype == "aws_s3_bucket":
            pab = res.get("public_access_block")
            if isinstance(pab, dict) and not all(pab.get(k) for k in PAB_FIELDS):
                files[filename] += f'''

resource "aws_s3_bucket_public_access_block" "{name}_public_access" {{
  bucket = aws_s3_bucket.{name}.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}}
'''
                related = _related(findings, address, "s3_public_access_block")
                addressed.update(id(f) for f in related)
                changes.append({
                    "kind": "s3_public_access_block", "resource": address, "file": filename,
                    "title": f"Block public access on {address}",
                    "explanation": (
                        "Adds an S3 public access block with all four settings on. New public ACLs and "
                        "bucket policies are rejected and existing public access stops working."
                    ),
                    "impact": "behavior_changing",
                    "risk": "Breaks anything that relies on this bucket being public (for example a static website).",
                    "current": {k: pab.get(k) for k in PAB_FIELDS},
                    "findings": [f.get("rule_id") for f in related],
                })
            if res.get("encryption_rules") == []:
                files[filename] += f'''

resource "aws_s3_bucket_server_side_encryption_configuration" "{name}_encryption" {{
  bucket = aws_s3_bucket.{name}.id

  rule {{
    apply_server_side_encryption_by_default {{
      sse_algorithm = "AES256"
    }}
  }}
}}
'''
                related = _related(findings, address, "s3_encryption")
                addressed.update(id(f) for f in related)
                changes.append({
                    "kind": "s3_encryption", "resource": address, "file": filename,
                    "title": f"Default encryption on {address}",
                    "explanation": (
                        "Adds SSE-S3 (AES256) default encryption. New objects are encrypted at rest; "
                        "existing objects are unchanged and clients need no changes."
                    ),
                    "impact": "safe",
                    "risk": "None expected.",
                    "current": {"encryption_rules": []},
                    "findings": [f.get("rule_id") for f in related],
                })

        elif rtype == "aws_instance" and res.get("metadata_http_tokens") == "optional":
            block = extract_resource_block(files[filename], rtype, name)
            hardened = block[:-1].rstrip() + '''

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }
}'''
            files[filename] = files[filename].replace(block, hardened, 1)
            related = _related(findings, address, "ec2_imdsv2")
            addressed.update(id(f) for f in related)
            changes.append({
                "kind": "ec2_imdsv2", "resource": address, "file": filename,
                "title": f"Require IMDSv2 on {address}",
                "explanation": (
                    "Sets http_tokens = \"required\": the instance metadata service only answers "
                    "session-token (IMDSv2) requests, which blocks SSRF-style credential theft."
                ),
                "impact": "behavior_changing",
                "risk": "Software on the instance that still uses IMDSv1 can no longer read instance credentials.",
                "current": {"http_tokens": "optional"},
                "findings": [f.get("rule_id") for f in related],
            })

    recommendations = [
        {
            "tool": f.get("tool"), "rule_id": f.get("rule_id"), "severity": f.get("severity"),
            "resource": f.get("resource"), "description": f.get("description"),
            "reason": "No automatic fix - review and change manually.",
        }
        for f in findings
        if f.get("severity") in ("CRITICAL", "HIGH") and id(f) not in addressed
    ]
    changed = {k: v for k, v in files.items() if v != terraform_files.get(k)}
    return {"files": changed, "changes": changes, "recommendations": recommendations}
