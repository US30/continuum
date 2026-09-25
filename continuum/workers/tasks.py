"""Arq worker: one job = one entity's case through the decisioning graph.

CLAUDE.md §4: workers on Arq/Redis. Run with:
    arq continuum.workers.tasks.WorkerSettings
The graph (deps, scorer, checkpointer connection) is built once per worker process in
`startup` and reused across jobs, so the trained model and episode features load once.

A case that needs a human comes back as `{"status": "interrupted", ...}` — it is parked in the
Redis checkpointer, not lost, and resumed later with continuum.graph.run.resume_case.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from typing import Any, ClassVar

from arq.connections import RedisSettings

from continuum.config.settings import get_settings
from continuum.graph.deps import GraphDeps
from continuum.graph.run import default_deps, redis_checkpointer, run_case
from continuum.guard.worker import dispatch_job
from continuum.telemetry.logging import configure_logging, get_logger
from continuum.telemetry.spans import configure_tracing

logger = get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    stack = ExitStack()
    ctx["stack"] = stack
    ctx["deps"] = default_deps(settings)
    ctx["checkpointer"] = stack.enter_context(redis_checkpointer(settings))
    configure_tracing(_span_processors())
    logger.info("worker.started")


async def shutdown(ctx: dict[str, Any]) -> None:
    ctx["stack"].close()


def _span_processors() -> list[Any]:
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    if get_settings().otel_console:
        return [SimpleSpanProcessor(ConsoleSpanExporter())]
    return []


async def run_case_job(ctx: dict[str, Any], entity_id: str) -> dict[str, Any]:
    deps: GraphDeps = ctx["deps"]
    result = run_case(uuid.UUID(entity_id), deps, ctx["checkpointer"])
    if "__interrupt__" in result:
        return {"status": "interrupted", "review": result["__interrupt__"][0].value}
    route = result["route"]
    priority = result.get("priority")
    dispatch = result.get("dispatch")
    if dispatch is not None:
        # Forward by task name: this module never imports continuum.guard.dispatch (I2).
        await ctx["redis"].enqueue_job(
            "dispatch_job", dispatch.verdict.model_dump_json(), dispatch.message.model_dump_json()
        )
    return {
        "status": "done",
        "dispatch_enqueued": dispatch is not None,
        "route": route.route,
        "guard": result["guard"].verdict.value if result.get("guard") else None,
        "expected_net_recovery": priority.expected_net_recovery if priority else None,
    }


def _redis_settings() -> RedisSettings:
    settings = get_settings()
    return RedisSettings(host=settings.redis_host, port=settings.redis_port)


class WorkerSettings:
    functions: ClassVar[list[Any]] = [run_case_job, dispatch_job]
    on_startup: ClassVar[Any] = startup
    on_shutdown: ClassVar[Any] = shutdown
    redis_settings: ClassVar[RedisSettings] = _redis_settings()
    max_jobs: ClassVar[int] = 4
    job_timeout: ClassVar[int] = 300
    keep_result: ClassVar[int] = 3600
