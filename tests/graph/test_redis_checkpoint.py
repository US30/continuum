"""CLAUDE.md Phase 5 done-when: an interrupted case resumes correctly from the Redis
checkpointer. The two `redis_checkpointer()` blocks are separate connections with separately
built graphs — nothing is carried over in memory, so the parked state must come from Redis."""

from __future__ import annotations

import pytest

from continuum.db.session import session_scope
from continuum.graph.deps import GraphDeps
from continuum.graph.nodes.route import ReviewDecision
from continuum.graph.run import redis_checkpointer, resume_case, run_case
from tests.graph.conftest import AS_OF, FakeLLM, make_entity

pytestmark = pytest.mark.integration


def test_interrupted_case_resumes_from_redis_with_fresh_connections(
    deps: GraphDeps, llm: FakeLLM
) -> None:
    with session_scope() as s:
        entity_id = make_entity(s, overdue="6000.00")  # high exposure -> guard ESCALATE

    with redis_checkpointer() as saver:
        parked = run_case(entity_id, deps, saver, AS_OF)
    assert "__interrupt__" in parked
    payload = parked["__interrupt__"][0].value
    assert payload["verdict"] == "escalate" and payload["may_approve"] is True
    calls_before_resume = len(llm.calls)

    with redis_checkpointer() as fresh_saver:  # new connection, new graph, no shared memory
        done = resume_case(
            entity_id, ReviewDecision(decision="approve", reviewer="pat"), deps, fresh_saver, AS_OF
        )

    assert done["route"].route == "ready_for_dispatch" and done["guard"].human_override
    assert len(llm.calls) == calls_before_resume  # earlier nodes were not re-run
    assert any(e.startswith("draft") for e in done["events"])  # ...but their state survived


def test_state_round_trips_through_redis_serialisation(deps: GraphDeps) -> None:
    with session_scope() as s:
        entity_id = make_entity(s, n_delinquent=2, overdue="6000.00")
    with redis_checkpointer() as saver:
        run_case(entity_id, deps, saver, AS_OF)
    with redis_checkpointer() as fresh:
        done = resume_case(
            entity_id, ReviewDecision(decision="reject", reviewer="pat"), deps, fresh, AS_OF
        )
    assert len(done["case"].accounts) == 2 and done["route"].route == "rejected_by_reviewer"
