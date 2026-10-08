from __future__ import annotations

import asyncio
import uuid

from continuum.workers import review_queue
from tests.fakes import FakeRedis

A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
B = uuid.UUID("00000000-0000-0000-0000-00000000000b")


def _payload(entity_id: uuid.UUID, overdue: float) -> dict[str, object]:
    return {"entity_id": str(entity_id), "verdict": "escalate", "total_overdue": overdue}


def test_pending_reviews_are_listed_largest_exposure_first() -> None:
    async def scenario() -> list[str]:
        redis = FakeRedis()
        await review_queue.add_pending(redis, A, _payload(A, 100.0))
        await review_queue.add_pending(redis, B, _payload(B, 9000.0))
        return [r["entity_id"] for r in await review_queue.list_pending(redis)]

    assert asyncio.run(scenario()) == [str(B), str(A)]


def test_adding_the_same_review_twice_keeps_one_entry() -> None:
    async def scenario() -> int:
        redis = FakeRedis()
        await review_queue.add_pending(redis, A, _payload(A, 100.0))
        await review_queue.add_pending(redis, A, _payload(A, 100.0))
        return len(await review_queue.list_pending(redis))

    assert asyncio.run(scenario()) == 1


def test_a_review_can_be_claimed_exactly_once() -> None:
    async def scenario() -> tuple[object, object, object]:
        redis = FakeRedis()
        await review_queue.add_pending(redis, A, _payload(A, 100.0))
        first = await review_queue.claim(redis, A)
        second = await review_queue.claim(redis, A)
        return first, second, await review_queue.get_pending(redis, A)

    first, second, remaining = asyncio.run(scenario())
    assert first == _payload(A, 100.0) and second is None and remaining is None
