"""Migration Safety and Security Posture are separate, and both fail closed."""

from tools.scores import migration_safety, security_posture

MANIFEST = {"resources_generated": 10}
PLAN_OK_CHECKS = [{"check_name": "init", "passed": True}, {"check_name": "show", "passed": True}]


def test_plan_with_no_changes_is_100_percent_safe():
    plan = {"skipped": False, "checks": PLAN_OK_CHECKS, "changes": [], "imported": 10}
    s = migration_safety(MANIFEST, plan, {}, verdict="PASS")
    assert s["score"] == 100 and s["status"] == "SAFE" and s["basis"] == "plan"
    assert s["imported"] == 10 and s["destroy_or_replace"] == 0


def test_in_place_update_lowers_score_but_is_not_destructive():
    plan = {"checks": PLAN_OK_CHECKS, "update": 1,
            "changes": [{"address": "aws_instance.web", "action": "update"}]}
    s = migration_safety(MANIFEST, plan, {}, verdict="NEEDS_APPROVAL")
    assert s["score"] == 90 and s["status"] == "CHANGES"
    assert s["changing_resources"] == ["aws_instance.web"]


def test_replace_is_destructive():
    plan = {"checks": PLAN_OK_CHECKS, "replace": 1,
            "changes": [{"address": "aws_db_instance.db", "action": "replace"}]}
    s = migration_safety(MANIFEST, plan, {})
    assert s["status"] == "DESTRUCTIVE" and s["destroy_or_replace"] == 1


def test_security_findings_never_move_migration_safety():
    plan = {"checks": PLAN_OK_CHECKS, "changes": []}
    assert migration_safety(MANIFEST, plan, {})["score"] == 100
    posture = security_posture({"critical_count": 2, "high_count": 3, "findings": [{}] * 5})
    assert posture["score"] == 20 and posture["rating"] == "POOR"


def test_drift_fallback_when_no_plan():
    drift = {"skipped": False, "resources_checked": 4, "resources_missing": 0, "findings": [
        {"resource": "aws_vpc.main", "tier": "destructive_equivalent"},
        {"resource": "aws_vpc.main", "tier": "informational"},
    ]}
    s = migration_safety(MANIFEST, {"skipped": True}, drift)
    assert s["basis"] == "drift" and s["score"] == 75 and s["status"] == "DESTRUCTIVE"


def test_no_evidence_is_unverified_not_100():
    s = migration_safety(MANIFEST, {"skipped": True}, {"skipped": True})
    assert s["score"] is None and s["status"] == "UNVERIFIED"


def test_failed_validation_is_unverified():
    plan = {"checks": PLAN_OK_CHECKS, "changes": []}
    assert migration_safety(MANIFEST, plan, {}, verdict="FAIL")["status"] == "UNVERIFIED"


def test_plan_that_did_not_finish_does_not_count():
    plan = {"checks": [{"check_name": "init", "passed": False}], "changes": []}
    assert migration_safety(MANIFEST, plan, {})["basis"] == "none"


def test_posture_unknown_when_no_scanner_ran():
    p = security_posture({"scanners_skipped": ["checkov", "trivy"], "scanners_failed": {"conftest": "timeout"}})
    assert p["score"] is None and p["rating"] == "UNKNOWN" and not p["complete"]


def test_posture_partial_is_flagged():
    p = security_posture({"scanners_skipped": ["trivy"], "findings": []})
    assert p["score"] == 100 and not p["complete"] and p["scanners_run"] == ["checkov", "conftest"]
