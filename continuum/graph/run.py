"""Run one case through the graph, and resume it after a human interrupt.

`thread_id` is deterministic per (entity, as-of date): re-running the same entity for the same
data date resumes/reuses one checkpointed thread instead of starting a second, which is what
makes a case *resume* after an interrupt or a crash rather than restart (CLAUDE.md §7, I6).
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.redis import RedisSaver
from langgraph.checkpoint.redis.jsonplus_redis import JsonPlusRedisSerializer
from langgraph.types import Command

from continuum.config.settings import Settings, get_settings
from continuum.db.session import session_scope
from continuum.graph import state as graph_state
from continuum.graph.build import build_graph
from continuum.graph.deps import DisabledLLM, GatewayLLM, GraphDeps
from continuum.graph.nodes.route import ReviewDecision
from continuum.graph.state import CaseState
from continuum.guard import models as guard_models
from continuum.guard.tone import DisabledToneChecker, LLMToneChecker
from continuum.scoring.runtime import Scorer
from continuum.screening import status as screening_status
from continuum.telemetry.logging import hash_pii, set_correlation_id


def thread_id_for(entity_id: uuid.UUID, as_of: date) -> str:
    return f"case:{entity_id}:{as_of.isoformat()}"


def correlation_id_for(thread_id: str) -> str:
    """What appears in logs and span attributes. The thread id embeds the entity id, so it is
    hashed (CLAUDE.md §10: PII hashed at write); the raw thread id stays in the checkpointer."""
    return f"case-{hash_pii(thread_id)}"


def default_deps(settings: Settings | None = None) -> GraphDeps:
    settings = settings or get_settings()
    return GraphDeps(
        session_scope=session_scope,
        scorer=Scorer.from_database(settings.data_as_of_date),
        llm=GatewayLLM() if settings.llm_enabled else DisabledLLM(),
        tone=LLMToneChecker() if settings.llm_enabled else DisabledToneChecker(),
        settings=settings,
    )


def checkpoint_serializer() -> JsonPlusRedisSerializer:
    """Serializer that revives our own models when a checkpoint is read back.

    LangGraph refuses to reconstruct arbitrary classes from stored JSON (a deliberate
    deserialization allowlist), so without this a resumed case would get plain dicts where it
    stored CaseView/Draft/GuardVerdict and fail validation. Only our state and guard contracts
    are allowed — an explicit list, not `allow everything`."""
    json_allowed: set[tuple[str, ...]] = set()
    msgpack_allowed: set[tuple[str, ...]] = set()
    for module in (graph_state, guard_models, screening_status):
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ == module.__name__:
                # JSON envelopes hold the dotted path split into parts; msgpack uses (module, name).
                json_allowed.add((*cls.__module__.split("."), cls.__name__))
                msgpack_allowed.add((cls.__module__, cls.__name__))
    return JsonPlusRedisSerializer(
        allowed_json_modules=json_allowed, allowed_msgpack_modules=msgpack_allowed
    )


@contextmanager
def redis_checkpointer(settings: Settings | None = None) -> Iterator[RedisSaver]:
    settings = settings or get_settings()
    with RedisSaver.from_conn_string(settings.redis_url) as saver:
        saver.serde = checkpoint_serializer()
        saver.setup()
        yield saver


def _config(entity_id: uuid.UUID, as_of: date) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id_for(entity_id, as_of)}}


def run_case(
    entity_id: uuid.UUID,
    deps: GraphDeps,
    checkpointer: BaseCheckpointSaver[Any],
    as_of: date | None = None,
) -> dict[str, Any]:
    """Runs (or continues) a case. If the guard needs a human, the returned dict carries an
    `__interrupt__` entry with the review payload and the run is parked in the checkpointer."""
    as_of = as_of or deps.settings.data_as_of_date
    thread = thread_id_for(entity_id, as_of)
    set_correlation_id(correlation_id_for(thread))
    graph = build_graph(deps, checkpointer)
    initial = CaseState(entity_id=entity_id, as_of=as_of, run_id=thread)
    result: dict[str, Any] = graph.invoke(initial, _config(entity_id, as_of))
    return result


def resume_case(
    entity_id: uuid.UUID,
    decision: ReviewDecision,
    deps: GraphDeps,
    checkpointer: BaseCheckpointSaver[Any],
    as_of: date | None = None,
) -> dict[str, Any]:
    """Resumes an interrupted case with a human decision. Works from a fresh process: the
    parked state comes from the checkpointer, not from memory."""
    as_of = as_of or deps.settings.data_as_of_date
    set_correlation_id(correlation_id_for(thread_id_for(entity_id, as_of)))
    graph = build_graph(deps, checkpointer)
    command: Command[Any] = Command(resume=decision.model_dump())
    result: dict[str, Any] = graph.invoke(command, _config(entity_id, as_of))
    return result
