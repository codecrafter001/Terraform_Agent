"""Real data behind the Terraform Runs, Change Requests and Settings pages.
In-memory SQLite from conftest; route functions are called directly (no Redis)."""

import asyncio
import json
import uuid

import pytest
from pydantic import ValidationError

from routers.jobs import list_jobs, list_runs
from routers.settings import WorkspaceDefaults, get_settings, update_defaults
from services.database import create_job_record, get_job_record, init_db, mark_job_complete
from tools.run_summary import build_runs_summary


@pytest.fixture(autouse=True)
def _db():
    init_db()


def _state(**extra):
    return {
        "status": "COMPLETE",
        "terraform_binary": "tofu",
        "verification_verdict": "PASS",
        "repair_attempts": 1,
        "agent_timings": {"validation_agent": 12.34, "plan_equivalence_agent": 40.0},
        "validation_results": {"passed": True, "checks": [
            {"check_name": "fmt", "passed": True, "output": "SECRET-LOOKING OUTPUT"},
            {"check_name": "init", "passed": True, "output": "x"},
            {"check_name": "validate", "passed": True, "output": "x"},
        ]},
        "plan_equivalence_results": {"create": 0, "update": 1, "replace": 0, "destroy": 0, "imported": 5,
                                     "checks": [{"check_name": "init", "passed": True},
                                                {"check_name": "plan", "passed": True, "output": "aws_s3_bucket.x"}]},
        "config_crosscheck": {"skipped": True, "reason": "runs with plan equivalence (opt-in)"},
        "verification_iterations": [
            {"iteration": 1, "verdict": "FAIL", "validation_passed": False, "incomplete_reasons": ["x"], "at": "t1"},
            {"iteration": 2, "verdict": "PASS", "validation_passed": True, "plan_changes": {"update": 1}, "at": "t2"},
        ],
        **extra,
    }


# --------------------------------------------------------------------------- runs


def test_runs_summary_keeps_commands_counts_and_timings_but_never_output():
    summary = build_runs_summary(_state())
    assert summary["engine"] == "tofu" and summary["verdict"] == "PASS" and summary["repair_attempts"] == 1
    stages = {s["stage"]: s for s in summary["stages"]}
    assert [c["name"] for c in stages["validation"]["commands"]] == ["fmt", "init", "validate"]
    assert stages["validation"]["seconds"] == 12.3
    assert stages["plan"]["counts"] == {"create": 0, "update": 1, "replace": 0, "destroy": 0, "imported": 5}
    assert stages["generate_config"]["skipped"] is True and stages["generate_config"]["commands"] == []
    assert [it["verdict"] for it in summary["iterations"]] == ["FAIL", "PASS"]
    dumped = json.dumps(summary)
    assert "SECRET-LOOKING OUTPUT" not in dumped and "aws_s3_bucket.x" not in dumped
    assert "incomplete_reasons" not in dumped


def test_no_runs_summary_when_no_terraform_command_ran():
    assert build_runs_summary({"status": "FAILED", "resources": []}) is None


def test_completed_job_is_listed_on_the_runs_page():
    job_id = f"job-{uuid.uuid4().hex[:10]}"
    create_job_record(job_id, "generate", "us-east-1", "2026-09-27T10:00:00")
    mark_job_complete(job_id, _state())
    [entry] = [r for r in asyncio.run(list_runs(limit=100)) if r["job_id"] == job_id]
    assert entry["runs"]["engine"] == "tofu" and entry["status"] == "COMPLETE"

    failed = f"job-{uuid.uuid4().hex[:10]}"
    create_job_record(failed, "generate", "us-east-1", "2026-09-27T10:00:00")
    mark_job_complete(failed, {"status": "FAILED"})
    assert failed not in {r["job_id"] for r in asyncio.run(list_runs(limit=100))}


# ------------------------------------------------------------------ change requests


def test_change_request_text_and_parsed_changes_are_kept_and_filterable():
    job_id = f"job-{uuid.uuid4().hex[:10]}"
    intent = {"requested_changes": [
        {"resource": "EC2 web", "attribute": "instance_type", "current_value": "t2.micro",
         "target_value": "t2.medium", "action": "resize", "ignored": "x"},
    ] + [{"resource": f"r{i}"} for i in range(30)]}
    create_job_record(job_id, "modify", "ap-south-1", "2026-09-27T10:00:00",
                      user_request="  Increase the web server to t2.medium  ", environment="staging",
                      analyzed_intent=intent)
    generate = f"job-{uuid.uuid4().hex[:10]}"
    create_job_record(generate, "generate", "ap-south-1", "2026-09-27T10:00:00")

    jobs = asyncio.run(list_jobs(limit=100, offset=0, include_archived=False, operation=["modify", "fix"]))
    ids = {j.job_id for j in jobs}
    assert job_id in ids and generate not in ids
    record = next(j for j in jobs if j.job_id == job_id)
    assert record.user_request == "Increase the web server to t2.medium" and record.environment == "staging"
    assert record.requested_changes[0] == {"resource": "EC2 web", "attribute": "instance_type",
                                           "current_value": "t2.micro", "target_value": "t2.medium", "action": "resize"}
    assert len(record.requested_changes) == 20  # capped


def test_job_without_a_request_has_no_change_data():
    job_id = f"job-{uuid.uuid4().hex[:10]}"
    create_job_record(job_id, "generate", "us-east-1", "2026-09-27T10:00:00", user_request="   ")
    record = get_job_record(job_id)
    assert record.user_request is None and record.requested_changes_summary is None


# ------------------------------------------------------------------------ settings


def test_settings_report_configuration_but_never_secret_values(monkeypatch):
    monkeypatch.setenv("INFRACOST_API_KEY", "ico-SENTINEL-VALUE")
    body = asyncio.run(get_settings())
    config = body["configuration"]
    assert config["integrations"]["infracost_api_key_set"] is True
    assert "ico-SENTINEL-VALUE" not in json.dumps(body)
    assert config["safety"]["blocked_terraform_commands"] == ["apply", "destroy", "import"]
    assert not {"apply", "destroy", "import"} & set(config["safety"]["allowed_terraform_subcommands"])
    assert set(config["tools"]) == {"terraform", "tofu", "checkov", "trivy", "conftest", "infracost"}


def test_defaults_round_trip():
    saved = asyncio.run(update_defaults(WorkspaceDefaults(
        region="eu-west-1", resource_filters=["S3", "VPC"], github_repo="acme/infra", base_branch="release/2026")))
    assert saved["defaults"]["resource_filters"] == ["VPC", "S3"]  # canonical order
    assert asyncio.run(get_settings())["defaults"]["github_repo"] == "acme/infra"
    asyncio.run(update_defaults(WorkspaceDefaults()))


@pytest.mark.parametrize("field, value", [
    ("region", "us-east-1; rm -rf /"),
    ("resource_filters", ["S3", "LAMBDA"]),
    ("github_repo", "https://github.com/acme/infra"),
    ("github_repo", "acme/infra/../x"),
    ("base_branch", "../main"),
    ("base_branch", "main branch"),
    ("terraform_binary", "/usr/bin/terraform"),
])
def test_invalid_defaults_are_rejected(field, value):
    with pytest.raises(ValidationError):
        WorkspaceDefaults(**{field: value})
