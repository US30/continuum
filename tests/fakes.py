"""In-memory stand-in for the Arq Redis pool: the hash commands the review queue uses, plus
`enqueue_job`, which records instead of queueing."""

from __future__ import annotations

from typing import Any


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.jobs: list[tuple[Any, ...]] = []

    async def hset(self, name: str, key: str, value: str) -> int:
        self.hashes.setdefault(name, {})[key] = value
        return 1

    async def hgetall(self, name: str) -> dict[str, str]:
        return dict(self.hashes.get(name, {}))

    async def hget(self, name: str, key: str) -> str | None:
        return self.hashes.get(name, {}).get(key)

    async def hdel(self, name: str, *keys: str) -> int:
        bucket = self.hashes.get(name, {})
        return sum(1 for key in keys if bucket.pop(key, None) is not None)

    async def enqueue_job(self, *args: Any) -> None:
        self.jobs.append(args)
