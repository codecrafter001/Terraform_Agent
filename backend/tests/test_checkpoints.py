"""A paused run's checkpoint never holds credentials, and resumes after a
round trip through its stored form (services/checkpoints.py)."""

import base64
import json

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import agents.graph as graph_mod
from services.checkpoints import export_thread, import_thread
from tests.test_graph_agent_loop import REVIEW_CLASSIFICATION, _stub, _validation_passing_after, calls  # noqa: F401

SECRETS = {
    "aws_access_key": "AKIAREALLOOKINGKEY123",
    "aws_secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYSECRETVALUE",
    "aws_session_token": "FQoGZXIvYXdzSESSIONTOKENVALUE",
    "zip_password": "zip-pass-do-not-store",
}


def _all_stored_bytes(exported: str) -> bytes:
    """Every serialized value in the export, decoded."""
    out = []

    def walk(node):
        if isinstance(node, list):
            if len(node) == 2 and all(isinstance(x, str) for x in node):
                try:
                    out.append(base64.b64decode(node[1], validate=True))
                except Exception:
                    pass
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            for x in node.values():
                walk(x)

    walk(json.loads(exported))
    return b"".join(out) + exported.encode()


async def test_paused_checkpoint_has_no_secrets_and_resumes(monkeypatch, calls):  # noqa: F811
    monkeypatch.setattr(graph_mod, "validation_agent_node",
                        _stub("validation_agent", calls, _validation_passing_after(0)))
    monkeypatch.setattr(graph_mod, "classification_agent_node",
                        _stub("classification_agent", calls, {"classification_results": REVIEW_CLASSIFICATION}))
    job_id = "job-ckpt"
    config = {"configurable": {"thread_id": job_id}}
    saver = InMemorySaver()
    app = graph_mod.build_graph(checkpointer=saver)
    await app.ainvoke(graph_mod.build_initial_state(job_id, dict(SECRETS)), config)
    assert (await app.aget_state(config)).tasks[0].interrupts  # paused at the gate

    exported = export_thread(saver, job_id)

    stored = _all_stored_bytes(exported)
    for secret in SECRETS.values():
        assert secret.encode() not in stored, secret

    # A fresh process: load the stored form and continue the same run.
    fresh = InMemorySaver()
    import_thread(fresh, job_id, exported)
    app2 = graph_mod.build_graph(checkpointer=fresh)
    await app2.ainvoke(Command(resume={"decision": "approved", "resource_decisions": {"role-1": "exclude"}}), config)
    final = (await app2.aget_state(config)).values
    assert final["status"] == "COMPLETE"
    assert final["aws_credentials"] == {} and final["zip_password"] is None
    assert calls.count("cloud_discovery") == 1  # nothing before the gate re-ran
