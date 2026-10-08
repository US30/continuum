"""Index of cases parked for a human (the review console's queue).

The parked case itself lives in the graph checkpointer; this is only the list of which
entities are waiting and what the reviewer needs to see, kept in a Redis hash next to the Arq
queue. It is an index, not the record: if it is lost, re-running the case for that entity
reaches the same interrupt and re-adds the entry (thread ids are deterministic, I6).

`claim` is the concurrency control: HDEL returns 1 for exactly one caller, so two reviewers
pressing a button at the same moment cannot both resume the same case.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

PENDING_KEY = "continuum:review:pending"


class HashStore(Protocol):
    """The three Redis hash commands used here (ArqRedis and redis.asyncio.Redis both fit)."""

    async def hset(self, name: str, key: str, value: str) -> Any: ...
    async def hgetall(self, name: str) -> Any: ...
    async def hget(self, name: str, key: str) -> Any: ...
    async def hdel(self, name: str, *keys: str) -> Any: ...


def _decode(raw: bytes | str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(raw)
    return payload


async def add_pending(redis: HashStore, entity_id: uuid.UUID, payload: dict[str, Any]) -> None:
    """Idempotent: one entry per entity, re-adding overwrites it with the same content."""
    await redis.hset(PENDING_KEY, str(entity_id), json.dumps(payload, sort_keys=True))


async def list_pending(redis: HashStore) -> list[dict[str, Any]]:
    """Every waiting review, largest exposure first (ties by entity id, so the order is stable)."""
    rows = [_decode(raw) for raw in (await redis.hgetall(PENDING_KEY)).values()]
    return sorted(rows, key=lambda r: (-float(r.get("total_overdue") or 0.0), r["entity_id"]))


async def get_pending(redis: HashStore, entity_id: uuid.UUID) -> dict[str, Any] | None:
    raw = await redis.hget(PENDING_KEY, str(entity_id))
    return _decode(raw) if raw is not None else None


async def claim(redis: HashStore, entity_id: uuid.UUID) -> dict[str, Any] | None:
    """Take a review off the queue. Returns its payload to exactly one caller, None to the rest."""
    payload = await get_pending(redis, entity_id)
    if payload is None or await redis.hdel(PENDING_KEY, str(entity_id)) != 1:
        return None
    return payload
