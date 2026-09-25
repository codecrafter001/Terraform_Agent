"""The live log SSE stream must replay a job's earlier lines to a client that
connects late, then continue live without duplicates.

Regression guard for the empty "Live Agent Stream" panel: logs were pub/sub
only, so everything published before the browser opened the stream (the
pipeline start and the discovery start line - i.e. the entire log of a job
stuck in discovery) was never shown.
"""

import asyncio
import json

from services.redis_client import RedisService


def _svc() -> RedisService:
    svc = RedisService()
    svc._redis_available = False  # in-memory mode, no Redis needed
    return svc


async def _collect(gen, n: int, timeout: float = 1.0):
    out = []

    async def run():
        async for item in gen:
            out.append(json.loads(item))
            if len(out) >= n:
                break

    await asyncio.wait_for(run(), timeout)
    return out


async def test_late_subscriber_gets_history_then_live_lines():
    svc = _svc()
    await svc.publish_log("job-1", "first", "system")
    await svc.publish_log("job-1", "second", "discovery")

    gen = svc.subscribe_logs("job-1")
    consumer = asyncio.create_task(_collect(gen, 3))
    await asyncio.sleep(0.01)
    await svc.publish_log("job-1", "third", "composer")
    lines = await consumer
    await gen.aclose()

    assert [line["message"] for line in lines] == ["first", "second", "third"]
    assert [line["seq"] for line in lines] == [1, 2, 3]


async def test_history_is_scrubbed_and_per_job():
    svc = _svc()
    await svc.publish_log("job-a", "key AKIAIOSFODNN7EXAMPLE leaked", "system")
    await svc.publish_log("job-b", "other job", "system")

    history = [json.loads(p) for p in await svc.get_log_history("job-a")]

    assert len(history) == 1
    assert "AKIAIOSFODNN7EXAMPLE" not in history[0]["message"]


async def test_replay_does_not_duplicate_lines_published_during_subscribe():
    svc = _svc()
    await svc.publish_log("job-2", "one", "system")
    gen = svc.subscribe_logs("job-2")
    first = await gen.__anext__()  # subscriber registered, history replay started
    # Simulate a line that lands in both the history and the live queue.
    await svc.publish_log("job-2", "two", "system")
    second = await asyncio.wait_for(gen.__anext__(), 1.0)
    await gen.aclose()

    assert json.loads(first)["message"] == "one"
    assert json.loads(second)["message"] == "two"
