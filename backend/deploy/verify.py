"""Verification of a rendered deployment project (design doc task 2.4).

Reuses migration mode's tools unchanged: TerraformRunner.validate_hcl
(fmt -check, init -backend=false, validate - through check_argv, no
credentials), the policy step's Checkov/Trivy/Conftest scan, the Security
Posture score, and Infracost for the whole new stack.

Fails closed, like the verifier: a scanner that is missing or crashed makes
the verdict INCOMPLETE, never PASS; a template that doesn't validate is FAIL
(that is a TerraAgent bug, not the user's).
"""

from typing import Any, Dict, List

from agents.policy_agent import policy_agent_node
from tools.credential_scrubber import CredentialScrubber
from tools.infracost_runner import InfracostRunner
from tools.scores import security_posture
from tools.terraform_runner import TerraformRunner

_MAX_CHECK_OUTPUT = 4000
_MAX_FINDINGS = 200


def _checks(validation: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "check_name": c.get("check_name"),
            "passed": bool(c.get("passed")),
            "output": CredentialScrubber.scrub_text(str(c.get("output") or ""))[-_MAX_CHECK_OUTPUT:],
        }
        for c in validation.get("checks", [])
    ]


async def verify(deployment_id: str, files: Dict[str, str], binary: str = "terraform") -> Dict[str, Any]:
    """`files`: the rendered .tf files plus terraform.tfvars.json."""
    tf_only = {name: content for name, content in files.items() if name.endswith(".tf")}

    validation = await TerraformRunner.validate_hcl(files, binary)
    checks = _checks(validation)
    system_failure = any(c["check_name"] == "system" for c in checks)

    policy = await policy_agent_node({"job_id": deployment_id, "terraform_files": tf_only})
    security = dict(policy["security_results"])
    findings = list(security.get("findings") or [])
    security["findings"] = findings[:_MAX_FINDINGS]
    security["findings_truncated"] = len(findings) > _MAX_FINDINGS
    posture = security_posture(security)

    cost = await InfracostRunner.estimate_cost(files)

    incomplete_reasons: List[str] = []
    if system_failure:
        incomplete_reasons.append("terraform validation could not run")
    for name in security.get("scanners_skipped") or []:
        incomplete_reasons.append(f"{name} is not installed")
    for name, error in (security.get("scanners_failed") or {}).items():
        incomplete_reasons.append(f"{name} failed: {error}")

    if not validation.get("passed") and not system_failure:
        verdict = "FAIL"
    elif incomplete_reasons:
        verdict = "INCOMPLETE"
    else:
        verdict = "PASS"

    return {
        "verdict": verdict,
        "incomplete_reasons": incomplete_reasons,
        "validation": {"passed": bool(validation.get("passed")), "checks": checks},
        "security": security,
        "security_posture": posture,
        "cost": cost,
    }
