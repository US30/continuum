"""The cloud checkpoint backend (Settings.checkpoint_backend = "postgres"): an interrupted case
resumes from Postgres with nothing carried over in memory — same contract as the Redis test."""

from __future__ import annotations

import pytest

from continuum.config.settings import Settings
from continuum.db.session import session_scope
from continuum.graph.deps import GraphDeps
from continuum.graph.nodes.route import ReviewDecision
from continuum.graph.run import checkpointer, postgres_checkpointer, resume_case, run_case
from tests.graph.conftest import AS_OF, FakeLLM, make_entity

pytestmark = pytest.mark.integration


def test_interrupted_case_resumes_from_postgres_with_fresh_connections(
    deps: GraphDeps, llm: FakeLLM
) -> None:
    with session_scope() as s:
        entity_id = make_entity(s, n_delinquent=2, overdue="6000.00")  # -> guard ESCALATE

    with postgres_checkpointer() as saver:
        parked = run_case(entity_id, deps, saver, AS_OF)
    assert "__interrupt__" in parked
    assert parked["__interrupt__"][0].value["may_approve"] is True
    calls_before_resume = len(llm.calls)

    with postgres_checkpointer() as fresh_saver:  # new connection, new graph, no shared memory
        done = resume_case(
            entity_id, ReviewDecision(decision="approve", reviewer="pat"), deps, fresh_saver, AS_OF
        )

    assert done["route"].route == "ready_for_dispatch" and done["guard"].human_override
    assert len(done["case"].accounts) == 2  # typed state survived serialisation
    assert len(llm.calls) == calls_before_resume  # earlier nodes were not re-run


def test_backend_setting_selects_the_saver() -> None:
    with checkpointer(Settings(checkpoint_backend="postgres")) as saver:
        assert type(saver).__name__ == "PostgresSaver"
    with checkpointer(Settings(checkpoint_backend="redis")) as saver:
        assert type(saver).__name__ == "RedisSaver"
