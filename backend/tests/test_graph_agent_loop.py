"""Routing tests for the four-agent graph (agents/graph.py).

Every underlying step is stubbed, so these exercise only the wiring:
Infrastructure -> IaC Engineering <-> Verification & Risk -> Delivery & Approval.
"""

from typing import Any, Dict, List

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import agents.graph as graph_mod


def _stub(name: str, calls: List[str], extra=None):
    async def step(state: Dict[str, Any]) -> Dict[str, Any]:
        calls.append(name)
        out: Dict[str, Any] = {"current_agent": name}
        if extra:
            out.update(extra(state) if callable(extra) else extra)
        return out
    return step


CLEAN_SCAN = {"security_results": {"findings": [], "scanners_skipped": [], "scanners_failed": {}}}


@pytest.fixture
def calls(monkeypatch) -> List[str]:
    calls: List[str] = []

    async def quiet(*args, **kwargs):
        return None

    # No Redis/log side effects in routing tests.
    monkeypatch.setattr(graph_mod, "_publish_state", quiet)
    monkeypatch.setattr(graph_mod, "_log", quiet)

    for name in (
        "intent_router_node", "resource_explorer_node", "graph_agent_node", "classification_agent_node",
        "adoption_planning_agent_node", "drift_reconciliation_agent_node",
        "plan_equivalence_agent_node", "config_crosscheck_node", "cost_agent_node", "hardening_agent_node",
    ):
        monkeypatch.setattr(graph_mod, name, _stub(name.replace("_node", ""), calls))
    monkeypatch.setattr(graph_mod, "cloud_discovery_node",
                        _stub("cloud_discovery", calls, {"resources": [{"id": "vpc-1"}]}))
    monkeypatch.setattr(graph_mod, "terraform_composer_node",
                        _stub("terraform_composer", calls, {"terraform_files": {"main.tf": "v0"}}))
    monkeypatch.setattr(graph_mod, "policy_agent_node", _stub("policy_agent", calls, CLEAN_SCAN))
    monkeypatch.setattr(graph_mod, "documentation_agent_node",
                        _stub("documentation_agent", calls, {"status": "COMPLETE", "zip_manifest": [{}]}))

    def repair(state):
        n = (state.get("repair_attempts") or 0) + 1
        history = list(state.get("repair_history") or []) + [
            {"cycle": n, "errors": 1, "fixed": ["aws_vpc.main"], "rejected": [], "unresolved": []}
        ]
        return {"repair_attempts": n, "terraform_files": {"main.tf": f"v{n}"}, "repair_history": history}
    monkeypatch.setattr(graph_mod, "repair_validation_node", _stub("repair_agent", calls, repair))
    return calls


def _validation_passing_after(n_repairs: int):
    def result(state):
        passed = (state.get("repair_attempts") or 0) >= n_repairs
        return {"validation_results": {"passed": passed, "checks": []}}
    return result


class Run:
    """One graph run with a checkpointer, so the risk gate's interrupt() can
    pause it and a decision can resume it - what services/pipeline.py does,
    minus Redis and the database."""

    def __init__(self, job_id: str = "job-test"):
        self.app = graph_mod.build_graph(checkpointer=InMemorySaver())
        self.config = {"configurable": {"thread_id": job_id}}

    async def start(self, **overrides) -> Dict[str, Any]:
        state = graph_mod.build_initial_state(self.config["configurable"]["thread_id"], {})
        state.update(overrides)
        await self.app.ainvoke(state, self.config)
        return await self.values()

    async def resume(self, decision: Dict[str, Any], update=None) -> Dict[str, Any]:
        await self.app.ainvoke(Command(resume=decision, update=update), self.config)
        return await self.values()

    async def values(self) -> Dict[str, Any]:
        snap = await self.app.aget_state(self.config)
        return {**snap.values, "_request": next(
            (i.value for t in snap.tasks for i in (t.interrupts or ())), None)}


async def _run(**overrides) -> Dict[str, Any]:
    return await Run().start(**overrides)


async def test_happy_path_runs_four_agents_once(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))

    final = await _run()

    assert "repair_agent" not in calls
    assert final["completed_stages"] == ["infrastructure", "iac_engineering", "verification", "delivery"]
    assert final["verification_verdict"] == "PASS"
    assert final["current_stage"] == "complete" and final["status"] == "COMPLETE"
    assert "verified" in final["stage_summaries"]["delivery"]


async def test_validation_failure_loops_back_to_iac_engineering_then_converges(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(1)))

    final = await _run()

    assert calls.count("repair_agent") == 1
    assert calls.count("terraform_composer") == 1  # repair, not regeneration
    assert [it["verdict"] for it in final["verification_iterations"]] == ["FAIL", "PASS"]
    assert final["repair_history"][0]["fixed"] == ["aws_vpc.main"]
    assert "Repair 1/2" in final["stage_summaries"]["iac_engineering"]


async def test_failed_validation_skips_later_checks(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(1)))

    final = await _run()

    first = final["verification_iterations"][0]
    assert first["checks_run"] == ["validate"]
    # drift/plan/policy ran exactly once - only on the pass that validated.
    assert calls.count("policy_agent") == 1 and calls.count("drift_reconciliation_agent") == 1


async def test_loop_is_bounded_by_max_repair_iterations(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(99)))

    final = await _run(max_repair_iterations=2)

    assert calls.count("repair_agent") == 2
    assert len(final["verification_iterations"]) == 3
    assert final["verification_verdict"] == "FAIL"
    # Stops and still delivers, clearly labelled - never loops forever.
    assert "documentation_agent" in calls
    assert "NOT verified" in final["stage_summaries"]["delivery"]


async def test_security_findings_are_reported_not_repaired(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))
    monkeypatch.setattr(graph_mod, "policy_agent_node", _stub("policy_agent", calls, {
        "security_results": {"high_count": 3, "critical_count": 1, "findings": [{}] * 4,
                             "scanners_skipped": [], "scanners_failed": {}}
    }))

    final = await _run()

    assert "repair_agent" not in calls
    assert final["verification_verdict"] == "PASS"
    assert final["verification_iterations"][0]["high_findings"] == 4


@pytest.mark.parametrize("security_results", [
    {"findings": [], "scanners_skipped": ["trivy"], "scanners_failed": {}},
    {"findings": [], "scanners_skipped": [], "scanners_failed": {"checkov": "TimeoutError: checkov timed out"}},
])
async def test_scanner_problems_fail_closed_as_incomplete(monkeypatch, calls, security_results):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))
    monkeypatch.setattr(graph_mod, "policy_agent_node",
                        _stub("policy_agent", calls, {"security_results": security_results}))

    final = await _run()

    assert final["verification_verdict"] == "INCOMPLETE"
    assert final["verification_iterations"][0]["incomplete_reasons"]
    assert "repair_agent" not in calls  # nothing a repair could fix
    assert "NOT fully verified" in final["stage_summaries"]["delivery"]


async def test_plan_init_failure_is_incomplete_not_clean(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))
    monkeypatch.setattr(graph_mod, "plan_equivalence_agent_node", _stub("plan_equivalence_agent", calls, {
        "plan_equivalence_results": {"checks": [{"check_name": "init", "passed": False}]}
    }))

    final = await _run()

    assert final["verification_verdict"] == "INCOMPLETE"


async def test_environment_failure_never_spends_a_repair_cycle(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node", _stub("validation_agent", calls, {
        "validation_results": {"passed": False, "checks": [{"check_name": "system", "passed": False, "output": "boom"}]}
    }))

    final = await _run()

    assert "repair_agent" not in calls
    assert final["verification_verdict"] == "INCOMPLETE"
    assert "documentation_agent" in calls


async def test_drift_approval_pauses_at_the_risk_gate(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))
    monkeypatch.setattr(graph_mod, "drift_reconciliation_agent_node", _stub("drift_reconciliation_agent", calls, {
        "pending_approval": {"reason": "drift", "findings": [DRIFT_FINDING]},
    }))

    final = await _run()

    # Nothing after drift runs and nothing is packaged: the run is paused.
    assert "plan_equivalence_agent" not in calls and "policy_agent" not in calls
    assert "repair_agent" not in calls
    assert "cost_agent" not in calls and "documentation_agent" not in calls
    assert final["_request"]["findings"] == [DRIFT_FINDING]
    assert final["current_stage"] == "awaiting_approval"
    assert final["verification_verdict"] == "NEEDS_APPROVAL"


DRIFT_FINDING = {"tool": "drift", "resource": "aws_vpc.main", "attribute": "cidr_block", "tier": "destructive"}


def _drift_once(calls):
    return _stub("drift_reconciliation_agent", calls, lambda state: {
        "pending_approval": {"reason": "drift", "findings": [DRIFT_FINDING]}})


async def test_approval_resumes_the_same_run_and_packages(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))
    monkeypatch.setattr(graph_mod, "drift_reconciliation_agent_node", _drift_once(calls))
    run = Run("job-approved")
    await run.start()

    final = await run.resume({"decision": "approved", "reason": "known and accepted"})

    assert final["_request"] is None
    assert final["status"] == "COMPLETE" and "documentation_agent" in calls
    assert final["approval_decision"]["decision"] == "approved"
    assert "approved by a human" in final["stage_summaries"]["delivery"]
    # Discovery and generation are not redone on resume.
    assert calls.count("cloud_discovery") == 1 and calls.count("terraform_composer") == 1


async def test_rejection_writes_the_audit_trail_only(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))
    monkeypatch.setattr(graph_mod, "drift_reconciliation_agent_node", _drift_once(calls))
    run = Run("job-rejected")
    await run.start()

    final = await run.resume({"decision": "rejected", "reason": "not ours"})

    assert final["status"] == "REJECTED"
    assert "documentation_agent" in calls and "cost_agent" not in calls
    assert final["approval_decision"]["reason"] == "not ours"


REVIEW_CLASSIFICATION = {"classifications": [
    {"resource_id": "vpc-1", "resource_type": "aws_vpc", "category": "unmanaged", "reason": [],
     "recommended_action": "import", "decision": "manage", "evidence": {}},
    {"resource_id": "role-1", "resource_type": "aws_iam_role", "category": "unmanaged", "reason": ["IAM"],
     "recommended_action": "manual_review", "decision": "review", "evidence": {"rule": "iam_requires_review"}},
], "summary": {}, "decisions": {}}


async def test_review_resources_pause_and_a_manage_decision_regenerates(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))
    monkeypatch.setattr(graph_mod, "classification_agent_node",
                        _stub("classification_agent", calls, {"classification_results": REVIEW_CLASSIFICATION}))
    run = Run("job-review")
    paused = await run.start()
    assert [r["resource_id"] for r in paused["_request"]["review_resources"]] == ["role-1"]
    assert paused["_request"]["review_resources"][0]["choices"] == ["manage", "reference", "exclude"]

    final = await run.resume({"decision": "approved", "resource_decisions": {"role-1": "manage"}})

    # Back through IaC Engineering and Verification with the new decision, then packaged.
    assert calls.count("terraform_composer") == 2 and calls.count("validation_agent") == 2
    assert calls.count("cloud_discovery") == 1
    role = next(c for c in final["classification_results"]["classifications"] if c["resource_id"] == "role-1")
    assert role["decision"] == "manage" and role["evidence"]["rule"] == "human_decision"
    assert final["status"] == "COMPLETE" and final["_request"] is None
    assert "regenerated after human Review decisions" in final["stage_summaries"]["iac_engineering"]


async def test_exclude_decision_packages_without_regenerating(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))
    monkeypatch.setattr(graph_mod, "classification_agent_node",
                        _stub("classification_agent", calls, {"classification_results": REVIEW_CLASSIFICATION}))
    run = Run("job-exclude")
    await run.start()

    final = await run.resume({"decision": "approved", "resource_decisions": {"role-1": "exclude"}})

    assert calls.count("terraform_composer") == 1
    assert final["status"] == "COMPLETE"
    assert final["classification_results"]["decisions"]["exclude"] == 1


async def test_approved_findings_are_not_asked_about_again(monkeypatch, calls):
    # The same drift finding shows up again on the re-verification after a
    # Review decision; the human already approved it, so the run doesn't stop.
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))
    monkeypatch.setattr(graph_mod, "classification_agent_node",
                        _stub("classification_agent", calls, {"classification_results": REVIEW_CLASSIFICATION}))
    monkeypatch.setattr(graph_mod, "drift_reconciliation_agent_node", _stub(
        "drift_reconciliation_agent", calls, lambda state: {"pending_approval": {
            "findings": ((state.get("pending_approval") or {}).get("findings") or []) + [DRIFT_FINDING]}}))
    run = Run("job-once")
    paused = await run.start()
    assert paused["_request"]["findings"] == [DRIFT_FINDING]

    final = await run.resume({"decision": "approved", "resource_decisions": {"role-1": "reference"}})

    assert final["_request"] is None and final["status"] == "COMPLETE"
    assert final["pending_approval"]["findings"] == [DRIFT_FINDING]  # kept once, for the audit trail
    # Pass 1 stopped at the unapproved finding; pass 2 wasn't cut short by the approved one.
    assert calls.count("drift_reconciliation_agent") == 2 and calls.count("policy_agent") == 1


async def test_resume_without_credentials_is_incomplete_when_live_checks_ran(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))
    monkeypatch.setattr(graph_mod, "classification_agent_node",
                        _stub("classification_agent", calls, {"classification_results": REVIEW_CLASSIFICATION}))
    monkeypatch.setattr(graph_mod, "drift_reconciliation_agent_node", _stub(
        "drift_reconciliation_agent", calls, {"drift_results": {"skipped": False, "resources_checked": 1}}))
    run = Run("job-nocreds")
    await run.start()

    final = await run.resume({"decision": "approved", "resource_decisions": {"role-1": "manage"}})
    assert final["verification_verdict"] == "INCOMPLETE"
    assert any("without AWS credentials" in r for r in final["verification_iterations"][-1]["incomplete_reasons"])


async def test_resume_with_resupplied_credentials_redoes_live_checks(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))
    monkeypatch.setattr(graph_mod, "classification_agent_node",
                        _stub("classification_agent", calls, {"classification_results": REVIEW_CLASSIFICATION}))
    monkeypatch.setattr(graph_mod, "drift_reconciliation_agent_node", _stub(
        "drift_reconciliation_agent", calls, {"drift_results": {"skipped": False, "resources_checked": 1}}))
    run = Run("job-creds")
    await run.start()

    final = await run.resume(
        {"decision": "approved", "resource_decisions": {"role-1": "manage"}},
        update={"aws_credentials": {"access_key": "AKIA", "secret_key": "s"}},
    )
    assert final["verification_verdict"] == "PASS"


async def test_heartbeat_logs_while_a_step_is_slow(monkeypatch):
    import asyncio

    logged: List[str] = []

    async def capture(job_id, stage, message):
        logged.append(message)

    async def slow():
        await asyncio.sleep(0.05)
        return {"ok": True}

    monkeypatch.setattr(graph_mod, "_log", capture)
    monkeypatch.setattr(graph_mod, "HEARTBEAT_INTERVAL_SECONDS", 0.01)

    result = await graph_mod._with_heartbeat("job-x", "infrastructure", "slow step", slow())

    assert result == {"ok": True}
    assert any("Still working: slow step" in m for m in logged)


async def test_callers_that_omit_new_state_keys_still_work(monkeypatch, calls):
    # LangGraph fills TypedDict keys the caller didn't supply with None - an
    # older/hand-built initial state must not crash the loop's arithmetic.
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(1)))
    state = graph_mod.build_initial_state("job-legacy", {})
    for key in ("max_repair_iterations", "repair_attempts", "verification_iterations", "repair_history",
                "verification_verdict", "completed_stages", "stage_summaries", "current_stage"):
        state.pop(key)

    run = Run("job-legacy")
    await run.app.ainvoke(state, run.config)
    final = await run.values()

    assert final["status"] == "COMPLETE"
    assert calls.count("repair_agent") == 1
