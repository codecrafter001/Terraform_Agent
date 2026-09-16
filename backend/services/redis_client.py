"""Async Redis Client wrapper with pub/sub, credential scrubbing, and in-memory fallback."""

import asyncio
import json
import logging
import os
from typing import Any, AsyncGenerator, Dict, Optional, Set

import redis.asyncio as aioredis

from tools.credential_scrubber import CredentialScrubber

logger = logging.getLogger(__name__)


class RedisService:
    """Async Redis wrapper for real-time SSE streaming and caching.

    Supports automatic in-memory fallback when Redis is offline or running in local dev mode.
    """

    def __init__(self, redis_url: Optional[str] = None):
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self._redis = None
        self._loop = None
        self._redis_available = True
        self._memory_state: Dict[str, Dict[str, Any]] = {}
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}

    async def get_client(self):
        if not self._redis_available:
            return None
        current_loop = asyncio.get_running_loop()
        if self._redis is None or self._loop is not current_loop:
            if self._redis is not None:
                try:
                    await self._redis.close()
                except Exception:
                    pass
            try:
                client = aioredis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=0.5,
                    socket_timeout=0.5
                )
                await client.ping()
                self._redis = client
                self._loop = current_loop
            except Exception as e:
                logger.info(f"Redis unavailable ({e}), using in-memory mode.")
                self._redis_available = False
                self._redis = None
                return None
        return self._redis


    async def publish_log(self, job_id: str, message: str, agent_name: str = "system") -> None:
        """Publishes scrubbed log lines to the pub/sub channel and local subscribers."""
        scrubbed_msg = CredentialScrubber.scrub_text(message)
        payload = json.dumps({
            "job_id": job_id,
            "agent": agent_name,
            "message": scrubbed_msg
        })

        # Deliver to in-memory subscribers
        if job_id in self._subscribers:
            for q in list(self._subscribers[job_id]):
                try:
                    q.put_nowait(payload)
                except Exception:
                    pass

        # Also publish to Redis if available
        try:
            client = await self.get_client()
            if client:
                await client.publish(f"job:{job_id}:logs", payload)
        except Exception as e:
            logger.debug(f"Redis publish unavailable, delivered in-memory: {e}")

    async def subscribe_logs(self, job_id: str) -> AsyncGenerator[str, None]:
        """Subscribes to log messages for SSE streaming."""
        queue: asyncio.Queue = asyncio.Queue()
        if job_id not in self._subscribers:
            self._subscribers[job_id] = set()
        self._subscribers[job_id].add(queue)

        pubsub = None
        reader_task = None
        try:
            client = await self.get_client()
            if client and self._redis_available:
                try:
                    pubsub = client.pubsub()
                    channel_name = f"job:{job_id}:logs"
                    await pubsub.subscribe(channel_name)

                    async def _redis_reader():
                        try:
                            async for msg in pubsub.listen():
                                if msg and msg.get("type") == "message":
                                    await queue.put(msg.get("data"))
                        except Exception:
                            pass

                    reader_task = asyncio.create_task(_redis_reader())
                except Exception:
                    pubsub = None
                    reader_task = None

            while True:
                data = await queue.get()
                yield data
        finally:
            # Without this, every disconnected SSE client leaked its background
            # _redis_reader() task (looping on pubsub.listen() forever) and its
            # Redis pub/sub subscription - unbounded over the life of the process.
            if reader_task is not None:
                reader_task.cancel()
                try:
                    await reader_task
                except (asyncio.CancelledError, Exception):
                    pass
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(f"job:{job_id}:logs")
                    await pubsub.close()
                except Exception:
                    pass

            if job_id in self._subscribers and queue in self._subscribers[job_id]:
                self._subscribers[job_id].remove(queue)
                if not self._subscribers[job_id]:
                    del self._subscribers[job_id]

    async def set_job_state(self, job_id: str, state_data: Dict[str, Any], expire_seconds: int = 86400) -> None:
        scrubbed = CredentialScrubber.scrub_dict(state_data)
        # Always update memory state
        self._memory_state[job_id] = scrubbed

        # Attempt Redis update
        try:
            client = await self.get_client()
            if client:
                await client.set(f"job:{job_id}:state", json.dumps(scrubbed), ex=expire_seconds)
        except Exception as e:
            logger.debug(f"Redis set_job_state falling back to in-memory: {e}")

    async def get_job_state(self, job_id: str) -> Dict[str, Any]:
        try:
            client = await self.get_client()
            if client:
                raw = await client.get(f"job:{job_id}:state")
                if raw:
                    return json.loads(raw)
        except Exception as e:
            logger.debug(f"Redis get_job_state falling back to in-memory: {e}")

        return self._memory_state.get(job_id, {})

    async def close(self):
        if self._redis:
            try:
                await self._redis.close()
            except Exception:
                pass


redis_service = RedisService()

