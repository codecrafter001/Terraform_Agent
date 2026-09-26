"""The two verification scores, kept deliberately separate.

Migration Safety - "will adopting this change anything?" Built only from
evidence about the adoption itself: the real plan (preferred), else the
live-vs-generated drift comparison. Security findings never move it.

Security Posture - "what's wrong with the current setup?" Built only from the
Checkov / Trivy / OPA findings on the generated code, which mirrors what is
live today. Adoption risk never moves it.

A bad security posture is not a reason to block an adoption (the findings go
into the adoption PR description and the optional Hardening PR), and a clean
security scan is not evidence that the import is safe. Mixing them in one
number hid both.

Both fail closed: when the evidence didn't exist or a tool didn't finish, the
score is None with a reason, never a reassuring 100.
"""

from typing import Any, Dict, List, Optional

SEVERITY_WEIGHTS: Dict[str, int] = {"CRITICAL": 25, "HIGH": 10, "MEDIUM": 3, "LOW": 1}
SCANNERS = ("checkov", "trivy", "conftest")


def _pct(good: int, total: int) -> int:
    return 100 if total <= 0 else round(100 * good / total)


def migration_safety(
    generation_manifest: Optional[Dict[str, Any]],
    plan_equivalence_results: Optional[Dict[str, Any]],
    drift_results: Optional[Dict[str, Any]],
    config_crosscheck: Optional[Dict[str, Any]] = None,
    verdict: Optional[str] = None,
) -> Dict[str, Any]:
    """Share of managed resources proven to adopt with zero changes.

    status: SAFE (nothing changes) | CHANGES (in-place updates only) |
    DESTRUCTIVE (any destroy/replace, or a destructive drift finding) |
    UNVERIFIED (no plan and no drift evidence, or validation never passed)."""
    manifest = generation_manifest or {}
    plan = plan_equivalence_results or {}
    drift = drift_results or {}
    cross = config_crosscheck or {}
    managed = int(manifest.get("resources_generated", 0) or 0)
    mismatches = len(cross.get("mismatches", []) or []) if cross and not cross.get("skipped") else None

    base: Dict[str, Any] = {
        "resources_managed": managed,
        "config_mismatches": mismatches,
        "changing_resources": [],
        "destroy_or_replace": 0,
    }

    if verdict == "FAIL":
        return {**base, "score": None, "status": "UNVERIFIED", "basis": "none",
                "reason": "the generated code never passed terraform validate"}

    plan_ran = bool(plan) and not plan.get("skipped") and any(
        c.get("check_name") == "show" and c.get("passed") for c in plan.get("checks", []) or []
    )
    if plan_ran:
        changes: List[Dict[str, str]] = list(plan.get("changes", []) or [])
        destructive = int(plan.get("replace", 0) or 0) + int(plan.get("destroy", 0) or 0)
        changing = sorted({c["address"] for c in changes if c.get("address")})
        unchanged = max(0, managed - len(changing))
        status = "DESTRUCTIVE" if destructive else ("CHANGES" if changing else "SAFE")
        return {
            **base,
            "score": _pct(unchanged, managed),
            "status": status,
            "basis": "plan",
            "no_op": unchanged,
            "changing_resources": changing,
            "destroy_or_replace": destructive,
            "imported": int(plan.get("imported", 0) or 0),
            "reason": "terraform plan with import blocks, against live AWS",
        }

    if drift and not drift.get("skipped"):
        checked = int(drift.get("resources_checked", 0) or 0)
        blocking = [
            f for f in drift.get("findings", []) or []
            if f.get("tier") in ("behavior_changing", "destructive_equivalent")
        ]
        missing = drift.get("resources_missing", 0)
        missing_count = len(missing) if isinstance(missing, list) else int(missing or 0)
        changing = sorted({str(f.get("resource") or f.get("resource_id")) for f in blocking})
        destructive = sum(1 for f in blocking if f.get("tier") == "destructive_equivalent") + missing_count
        unchanged = max(0, checked - len(changing) - missing_count)
        status = "DESTRUCTIVE" if destructive else ("CHANGES" if changing else "SAFE")
        return {
            **base,
            "score": _pct(unchanged, checked) if checked else None,
            "status": status if checked else "UNVERIFIED",
            "basis": "drift",
            "no_op": unchanged,
            "changing_resources": changing,
            "destroy_or_replace": destructive,
            "reason": (
                "live AWS attributes compared with the generated HCL (no terraform plan was run)"
                if checked else "no managed resource could be compared with live AWS"
            ),
        }

    return {**base, "score": None, "status": "UNVERIFIED", "basis": "none",
            "reason": "neither a terraform plan nor a drift comparison ran (no AWS credentials)"}


def security_posture(security_results: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """100 minus weighted findings (CRITICAL 25, HIGH 10, MEDIUM 3, LOW 1),
    floored at 0. None when no scanner produced a result."""
    sec = security_results or {}
    skipped = list(sec.get("scanners_skipped", []) or [])
    failed = dict(sec.get("scanners_failed", {}) or {})
    ran = [s for s in SCANNERS if s not in skipped and s not in failed] if sec else []
    counts = {sev.lower(): int(sec.get(f"{sev.lower()}_count", 0) or 0) for sev in SEVERITY_WEIGHTS}

    if not ran:
        return {"score": None, "rating": "UNKNOWN", "complete": False, "scanners_run": [],
                "scanners_missing": skipped, "scanners_failed": failed, "counts": counts,
                "total_findings": 0, "reason": "no security scanner produced a result"}

    penalty = sum(SEVERITY_WEIGHTS[sev] * counts[sev.lower()] for sev in SEVERITY_WEIGHTS)
    score = max(0, 100 - penalty)
    complete = not skipped and not failed
    return {
        "score": score,
        "rating": "GOOD" if score >= 90 else "FAIR" if score >= 70 else "POOR",
        "complete": complete,
        "scanners_run": ran,
        "scanners_missing": skipped,
        "scanners_failed": failed,
        "counts": counts,
        "total_findings": len(sec.get("findings", []) or []),
        "reason": (
            "Checkov, Trivy and OPA findings on the current configuration"
            if complete else "partial: not every scanner completed, so findings may be missing"
        ),
    }
