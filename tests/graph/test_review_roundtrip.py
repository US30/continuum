"""Worker side of the review console: a parked case is listed for review, a reviewer's decision
resumes it and forwards the dispatch, and a failed resume puts the review back on the queue."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from continuum.api.casefile import load_case_file
from continuum.db.session import session_scope
from continuum.graph.deps import GraphDeps
from continuum.workers import review_queue, tasks
from continuum.workers.tasks import resume_case_job, run_case_job
from tests.fakes import FakeRedis
from tests.graph.conftest import make_entity

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _fixed_model_mtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "_model_mtime", lambda: 1.0)


def _ctx(deps: GraphDeps) -> dict[str, Any]:
    return {
        "deps": deps,
        "model_mtime": 1.0,
        "checkpointer": InMemorySaver(),
        "redis": FakeRedis(),
    }


def test_escalated_case_is_queued_then_approved_and_dispatched(deps: GraphDeps) -> None:
    with session_scope() as s:
        entity_id = make_entity(s, overdue="6000.00")  # high exposure -> guard ESCALATE
    ctx = _ctx(deps)

    parked = asyncio.run(run_case_job(ctx, str(entity_id)))
    assert parked["status"] == "interrupted"
    (pending,) = asyncio.run(review_queue.list_pending(ctx["redis"]))
    assert pending["entity_id"] == str(entity_id) and pending["may_approve"] is True

    claimed = asyncio.run(review_queue.claim(ctx["redis"], entity_id))
    done = asyncio.run(
        resume_case_job(ctx, str(entity_id), "approve", "pat@example.com", json.dumps(claimed))
    )
    assert done["route"] == "ready_for_dispatch" and done["dispatch_enqueued"] is True
    assert [job[0] for job in ctx["redis"].jobs] == ["dispatch_job"]
    assert asyncio.run(review_queue.list_pending(ctx["redis"])) == []


def test_failed_resume_puts_the_review_back(deps: GraphDeps) -> None:
    ctx = _ctx(deps)
    review = {"entity_id": str(uuid.uuid4()), "verdict": "escalate", "total_overdue": 1.0}
    with pytest.raises(ValueError):
        asyncio.run(
            resume_case_job(ctx, review["entity_id"], "send_anyway", "pat", json.dumps(review))
        )
    assert asyncio.run(review_queue.list_pending(ctx["redis"])) == [review]


def test_case_file_shows_every_account_of_the_entity(deps: GraphDeps) -> None:
    with session_scope() as s:
        entity_id = make_entity(s, n_delinquent=3)
    with session_scope() as s:
        case_file = load_case_file(s, entity_id)
    assert len(case_file.accounts) == 3 and case_file.screening == "clear"


def test_new_model_artifact_rebuilds_the_deps(
    deps: GraphDeps, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(deps)
    assert tasks._current_deps(ctx) is deps  # unchanged artifact: same deps

    rebuilt = GraphDeps(deps.session_scope, deps.scorer, deps.llm, deps.tone, deps.settings)
    monkeypatch.setattr(tasks, "_model_mtime", lambda: 2.0)
    monkeypatch.setattr(tasks, "default_deps", lambda settings: rebuilt)
    assert tasks._current_deps(ctx) is rebuilt and ctx["model_mtime"] == 2.0
