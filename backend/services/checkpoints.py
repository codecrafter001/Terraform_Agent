"""Durable LangGraph checkpoints for jobs paused at the approval gate.

The graph runs with an in-memory checkpointer. When it pauses at the
Delivery & Approval Agent's `interrupt()`, that job's thread is exported to
one row in the jobs database; POST /scan/{id}/approve|reject loads it back
into a fresh in-memory checkpointer (in whichever process picks the resume
up) and continues the same run with `Command(resume=...)`.

CLAUDE.md rule #4: credentials never reach the database. The export replaces
the credential channels outright and walks every other stored value to drop
any credential-named key (the graph's input is stored as one dict write, for
instance), so the saved row carries no secrets. The flip side is that a
resumed run has no AWS credentials unless the approver re-supplies them - the
verifier then fails closed (INCOMPLETE) for any live-AWS check it can't redo.

Why not a Postgres/SQLite checkpointer package: DATABASE_URL may be either,
and only paused jobs need to survive a process boundary - one row per paused
job, deleted once the job finishes, is all that's required.
"""

import base64
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from langgraph.checkpoint.memory import InMemorySaver

from models.orm import GraphCheckpoint
from services.database import SessionLocal

logger = logging.getLogger("terraagent.checkpoints")

# Graph channels that hold secrets, and the harmless value stored instead.
SECRET_CHANNELS: Dict[str, Any] = {"aws_credentials": {}, "zip_password": None}
# Any dict key with one of these names is dropped from every stored value.
SECRET_KEYS = frozenset({
    "aws_credentials", "zip_password", "aws_access_key", "aws_secret_key", "aws_session_token",
    "access_key", "secret_key", "session_token", "github_token",
})


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (SECRET_CHANNELS.get(k) if k in SECRET_KEYS else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub(v) for v in value)
    return value


def _b64(typed: Tuple[str, bytes]) -> List[str]:
    return [typed[0], base64.b64encode(typed[1]).decode("ascii")]


def _unb64(item: List[str]) -> Tuple[str, bytes]:
    return item[0], base64.b64decode(item[1])


def export_thread(saver: InMemorySaver, thread_id: str) -> str:
    """Serialize one thread's checkpoints, writes and channel blobs to JSON,
    with every secret removed."""
    serde = saver.serde

    def clean(typed: Tuple[str, bytes], channel: Optional[str] = None) -> List[str]:
        if channel in SECRET_CHANNELS:
            return _b64(serde.dumps_typed(SECRET_CHANNELS[channel]))
        if typed[0] == "empty":
            return _b64(typed)
        return _b64(serde.dumps_typed(_scrub(serde.loads_typed(typed))))

    # The checkpoint record itself is bookkeeping only - InMemorySaver keeps
    # channel values in blobs - and its version maps are keyed by channel name,
    # so it is stored as-is (scrubbing would clobber the "aws_credentials" key
    # of channel_versions). Values live in blobs and writes, scrubbed below.
    storage = [
        [ns, cid, _b64(ckpt), clean(meta), parent]
        for ns, checkpoints in saver.storage.get(thread_id, {}).items()
        for cid, (ckpt, meta, parent) in checkpoints.items()
    ]
    writes = [
        [ns, cid, list(inner), task_id, channel, clean(value, channel), task_path]
        for (tid, ns, cid), entries in saver.writes.items() if tid == thread_id
        for inner, (task_id, channel, value, task_path) in entries.items()
    ]
    blobs = [
        [ns, channel, version, clean(value, channel)]
        for (tid, ns, channel, version), value in saver.blobs.items() if tid == thread_id
    ]
    return json.dumps({"storage": storage, "writes": writes, "blobs": blobs})


def import_thread(saver: InMemorySaver, thread_id: str, data: str) -> None:
    payload = json.loads(data)
    for ns, cid, ckpt, meta, parent in payload["storage"]:
        saver.storage[thread_id][ns][cid] = (_unb64(ckpt), _unb64(meta), parent)
    for ns, cid, inner, task_id, channel, value, task_path in payload["writes"]:
        saver.writes[(thread_id, ns, cid)][tuple(inner)] = (task_id, channel, _unb64(value), task_path)
    for ns, channel, version, value in payload["blobs"]:
        saver.blobs[(thread_id, ns, channel, version)] = _unb64(value)


def save_checkpoint(job_id: str, data: str) -> None:
    with SessionLocal() as db:
        row = db.get(GraphCheckpoint, job_id)
        now = datetime.utcnow().isoformat()
        if row:
            row.data, row.updated_at = data, now
        else:
            db.add(GraphCheckpoint(job_id=job_id, data=data, updated_at=now))
        db.commit()


def load_checkpoint(job_id: str) -> Optional[str]:
    with SessionLocal() as db:
        row = db.get(GraphCheckpoint, job_id)
        return row.data if row else None


def delete_checkpoint(job_id: str) -> None:
    try:
        with SessionLocal() as db:
            row = db.get(GraphCheckpoint, job_id)
            if row:
                db.delete(row)
                db.commit()
    except Exception as e:  # best-effort cleanup - a leftover row holds no secrets
        logger.warning(f"[{job_id}] could not delete checkpoint: {e}")
