"""Routing tests for the four-agent graph (agents/graph.py).

Every underlying step is stubbed, so these exercise only the wiring: the
Verifier <-> Repair loop, its iteration bound, and the human-approval halts
that must survive the 13-node -> 4-agent restructure unchanged.
"""

from typing import Any, Dict, List

import pytest

import agents.graph as graph_mod


def _stub(name: str, calls: List[str], extra=None):
    async def step(state: Dict[str, Any]) -> Dict[str, Any]:
        calls.append(name)
        out = {"current_agent": name}
        if extra:
            out.update(extra(state) if callable(extra) else extra)
        return out
    return step


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
        "plan_equivalence_agent_node", "cost_agent_node",
    ):
        monkeypatch.setattr(graph_mod, name, _stub(name.replace("_node", ""), calls))
    monkeypatch.setattr(graph_mod, "cloud_discovery_node",
                        _stub("cloud_discovery", calls, {"resources": [{"id": "vpc-1"}]}))
    monkeypatch.setattr(graph_mod, "terraform_composer_node",
                        _stub("terraform_composer", calls, {"terraform_files": {"main.tf": "v0"}}))
    monkeypatch.setattr(graph_mod, "policy_agent_node",
                        _stub("policy_agent", calls, {"security_results": {"findings": []}}))
    monkeypatch.setattr(graph_mod, "documentation_agent_node",
                        _stub("documentation_agent", calls, {"status": "COMPLETE", "zip_manifest": [{}]}))

    def repair(state):
        n = (state.get("repair_attempts") or 0) + 1
        return {"repair_attempts": n, "terraform_files": {"main.tf": f"v{n}"}}
    monkeypatch.setattr(graph_mod, "repair_agent_node", _stub("repair_agent", calls, repair))
    return calls


def _validation_passing_after(n_repairs: int):
    def result(state):
        passed = (state.get("repair_attempts") or 0) >= n_repairs
        return {"validation_results": {"passed": passed, "checks": []}}
    return result


async def _run(**overrides) -> Dict[str, Any]:
    state = graph_mod.build_initial_state("job-test", {})
    state.update(overrides)
    return await graph_mod.build_graph().ainvoke(state)


async def test_loop_repairs_then_converges(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(1)))

    final = await _run()

    assert calls.count("repair_agent") == 1
    assert calls.count("validation_agent") == 2
    assert [it["passed"] for it in final["verification_iterations"]] == [False, True]
    assert final["completed_stages"] == ["discovery", "composer", "verifier", "repair", "package"]
    assert final["current_stage"] == "complete"
    assert final["status"] == "COMPLETE"
    assert "patched 1 file" in final["stage_summaries"]["repair"]


async def test_loop_is_bounded_by_max_repair_iterations(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(99)))

    final = await _run(max_repair_iterations=2)

    assert calls.count("repair_agent") == 2
    assert len(final["verification_iterations"]) == 3
    assert not final["verification_iterations"][-1]["passed"]
    # Stops and still packages, reporting what's failing - never loops forever.
    assert "documentation_agent" in calls


async def test_high_security_finding_triggers_repair(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))
    monkeypatch.setattr(graph_mod, "policy_agent_node", _stub("policy_agent", calls, lambda s: {
        "security_results": {"high_count": 0 if s.get("repair_attempts", 0) else 1, "findings": []}
    }))

    final = await _run()

    assert calls.count("repair_agent") == 1
    assert final["verification_iterations"][0]["high_findings"] == 1
    assert final["verification_iterations"][1]["passed"]


async def test_drift_approval_halts_mid_verification(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))
    monkeypatch.setattr(graph_mod, "drift_reconciliation_agent_node", _stub("drift_reconciliation_agent", calls, {
        "pending_approval": {"reason": "drift", "findings": [{"tier": "destructive"}]},
        "status": "AWAITING_APPROVAL",
    }))

    final = await _run()

    # Same halt the old drift_gate edge made: nothing after drift runs.
    assert "plan_equivalence_agent" not in calls
    assert "policy_agent" not in calls
    assert "repair_agent" not in calls
    assert "cost_agent" not in calls and "documentation_agent" not in calls
    assert final["status"] == "AWAITING_APPROVAL"
    assert final["current_stage"] == "awaiting_approval"
    assert final["verification_iterations"][0]["halted_for_approval"]


async def test_repair_escalation_halts_instead_of_reverifying(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(99)))
    monkeypatch.setattr(graph_mod, "repair_agent_node", _stub("repair_agent", calls, {
        "repair_attempts": 1,
        "pending_approval": {"reason": "repair_requires_human_approval", "findings": [{"tier": "behavior_changing"}]},
        "status": "AWAITING_APPROVAL",
    }))

    final = await _run()

    assert calls.count("validation_agent") == 1
    assert "documentation_agent" not in calls
    assert final["current_stage"] == "awaiting_approval"


async def test_environment_failure_never_spends_a_repair_cycle(monkeypatch, calls):
    monkeypatch.setattr(graph_mod, "validation_agent_node", _stub("validation_agent", calls, {
        "validation_results": {"passed": False, "checks": [{"check_name": "system", "passed": False}]}
    }))

    final = await _run()

    assert "repair_agent" not in calls
    assert final["verification_iterations"][0]["system_failure"]
    assert "documentation_agent" in calls


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

    result = await graph_mod._with_heartbeat("job-x", "discovery", "slow step", slow())

    assert result == {"ok": True}
    assert any("Still working: slow step" in m for m in logged)


async def test_callers_that_omit_new_state_keys_still_work(monkeypatch, calls):
    # LangGraph fills TypedDict keys the caller didn't supply with None - an
    # older/hand-built initial state (like test_langgraph_pipeline's) must not
    # crash the loop's arithmetic.
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(1)))
    state = graph_mod.build_initial_state("job-legacy", {})
    for key in ("max_repair_iterations", "repair_attempts", "verification_iterations",
                "completed_stages", "stage_summaries", "current_stage"):
        state.pop(key)

    final = await graph_mod.build_graph().ainvoke(state)

    assert final["status"] == "COMPLETE"
    assert calls.count("repair_agent") == 1
