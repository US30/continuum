"""Arq worker: one job = one entity's case through the decisioning graph.

CLAUDE.md §4: workers on Arq/Redis. Run with:
    arq continuum.workers.tasks.WorkerSettings
The graph (deps, scorer, checkpointer connection) is built once per worker process in
`startup` and reused across jobs, so the trained model and episode features load once.

A case that needs a human comes back as `{"status": "interrupted", ...}` — it is parked in the
checkpointer, not lost, and listed in continuum.workers.review_queue for the review console.
A reviewer's decision arrives as `resume_case_job`, which resumes the parked case.
"""

from __future__ import annotations

import json
import uuid
from contextlib import ExitStack
from typing import Any, ClassVar

from arq.connections import RedisSettings

from continuum.config.settings import get_settings
from continuum.graph.deps import GraphDeps
from continuum.graph.nodes.route import ReviewDecision
from continuum.graph.run import checkpointer, default_deps, resume_case, run_case
from continuum.guard.worker import dispatch_job
from continuum.scoring.train import MODEL_PATH
from continuum.telemetry.logging import configure_logging, get_logger
from continuum.telemetry.spans import configure_tracing
from continuum.workers import review_queue

logger = get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    stack = ExitStack()
    ctx["stack"] = stack
    ctx["deps"] = default_deps(settings)
    ctx["model_mtime"] = _model_mtime()
    ctx["checkpointer"] = stack.enter_context(checkpointer(settings))
    configure_tracing(_span_processors())
    logger.info("worker.started")


def _model_mtime() -> float:
    return MODEL_PATH.stat().st_mtime


def _current_deps(ctx: dict[str, Any]) -> GraphDeps:
    """The graph deps, rebuilt when the nightly batch has written a new model artifact. The
    scorer holds the model and the whole book's features in memory, so a long-running worker
    would otherwise keep scoring cases with last night's model and data."""
    mtime = _model_mtime()
    if mtime != ctx["model_mtime"]:
        ctx["deps"] = default_deps(get_settings())
        ctx["model_mtime"] = mtime
        logger.info("worker.model_reloaded")
    deps: GraphDeps = ctx["deps"]
    return deps


async def shutdown(ctx: dict[str, Any]) -> None:
    ctx["stack"].close()


def _span_processors() -> list[Any]:
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )

    settings = get_settings()
    processors: list[Any] = []
    if settings.otel_console:
        processors.append(SimpleSpanProcessor(ConsoleSpanExporter()))
    if settings.otel_exporter_otlp_endpoint:
        # Cloud: an OpenTelemetry collector sidecar receives OTLP and forwards to Cloud Trace.
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)
        processors.append(BatchSpanProcessor(exporter))
    return processors


async def _finish(
    ctx: dict[str, Any], entity_id: uuid.UUID, result: dict[str, Any]
) -> dict[str, Any]:
    """Shared tail of a run and a resume: park for review, or forward a cleared dispatch."""
    if "__interrupt__" in result:
        review = result["__interrupt__"][0].value
        await review_queue.add_pending(ctx["redis"], entity_id, review)
        return {"status": "interrupted", "review": review}
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


async def run_case_job(ctx: dict[str, Any], entity_id: str) -> dict[str, Any]:
    entity = uuid.UUID(entity_id)
    return await _finish(ctx, entity, run_case(entity, _current_deps(ctx), ctx["checkpointer"]))


async def resume_case_job(
    ctx: dict[str, Any], entity_id: str, decision: str, reviewer: str, review_json: str
) -> dict[str, Any]:
    """Resume a parked case with a reviewer's decision. The console already claimed the review
    (took it off the queue); if resuming fails it is put back so the case is not lost."""
    deps = _current_deps(ctx)
    entity = uuid.UUID(entity_id)
    try:
        review_decision = ReviewDecision.model_validate(
            {"decision": decision, "reviewer": reviewer}
        )
        result = resume_case(entity, review_decision, deps, ctx["checkpointer"])
    except Exception:
        await review_queue.add_pending(ctx["redis"], entity, json.loads(review_json))
        raise
    return await _finish(ctx, entity, result)


def _redis_settings() -> RedisSettings:
    settings = get_settings()
    return RedisSettings(host=settings.redis_host, port=settings.redis_port)


class WorkerSettings:
    functions: ClassVar[list[Any]] = [run_case_job, resume_case_job, dispatch_job]
    on_startup: ClassVar[Any] = startup
    on_shutdown: ClassVar[Any] = shutdown
    redis_settings: ClassVar[RedisSettings] = _redis_settings()
    max_jobs: ClassVar[int] = 4
    job_timeout: ClassVar[int] = 300
    keep_result: ClassVar[int] = 3600
