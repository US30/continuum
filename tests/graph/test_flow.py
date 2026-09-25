"""The decisioning graph end to end: real SQL case assembly and routing, fake model/scorer."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import func, select

from continuum.db.models.collections import Case, CaseDisposition
from continuum.db.models.entity import EdgeType
from continuum.db.models.risk_file import RiskFileEvent, RiskFileEventType
from continuum.db.session import session_scope
from continuum.graph.deps import GraphDeps
from continuum.graph.nodes.route import ReviewDecision
from continuum.graph.run import resume_case, run_case
from continuum.graph.schemas import FraudAdjudication
from continuum.guard.models import Verdict
from tests.graph.conftest import AS_OF, FakeLLM, FakeScorer, link, make_entity

pytestmark = pytest.mark.integration


def run(
    entity_id: uuid.UUID, deps: GraphDeps, saver: InMemorySaver | None = None
) -> dict[str, Any]:
    return run_case(entity_id, deps, saver or InMemorySaver(), AS_OF)


def interrupted(result: dict[str, Any]) -> bool:
    return "__interrupt__" in result


class TestHappyPath:
    def test_clear_case_passes_the_guard_and_is_ready_for_dispatch(
        self, deps: GraphDeps, llm: FakeLLM
    ) -> None:
        with session_scope() as s:
            entity_id = make_entity(s)
        result = run(entity_id, deps)

        assert result["route"].route == "ready_for_dispatch"
        assert result["guard"].verdict is Verdict.PASS
        assert "STOP" in result["draft"].body
        assert llm.nodes_called() == ["strategy", "draft"]  # 2 options -> model picks; then draft

    def test_case_and_risk_file_event_are_written_once(self, deps: GraphDeps) -> None:
        with session_scope() as s:
            entity_id = make_entity(s)
        saver = InMemorySaver()
        run(entity_id, deps, saver)
        run(entity_id, deps, saver)  # rerun on the same thread: no duplicate rows (I6)

        with session_scope() as s:
            dispositions = list(
                s.scalars(select(Case.disposition).where(Case.entity_id == entity_id))
            )
            events = s.scalar(
                select(func.count())
                .select_from(RiskFileEvent)
                .where(
                    RiskFileEvent.entity_id == entity_id,
                    RiskFileEvent.event_type == RiskFileEventType.CASE_DISPOSITION,
                )
            )
        assert dispositions == [CaseDisposition.OPEN]
        assert events == 1


class TestCollapse:
    def test_three_delinquent_accounts_become_one_case_and_one_outreach(
        self, deps: GraphDeps, llm: FakeLLM
    ) -> None:
        with session_scope() as s:
            entity_id = make_entity(s, n_delinquent=3, overdue="100.00")
        result = run(entity_id, deps)

        assert len(result["case"].accounts) == 3
        assert result["case"].total_overdue == pytest.approx(300.0)
        assert llm.nodes_called().count("draft") == 1  # one message, not three
        with session_scope() as s:
            n_cases = s.scalar(
                select(func.count()).select_from(Case).where(Case.entity_id == entity_id)
            )
        assert n_cases == 1


class TestRouting:
    def test_no_delinquent_account_short_circuits(self, deps: GraphDeps, llm: FakeLLM) -> None:
        with session_scope() as s:
            entity_id = make_entity(s, n_delinquent=0)
        result = run(entity_id, deps)
        assert result["route"].route == "no_delinquency" and llm.calls == []

    def test_negative_expected_uplift_means_no_contact_and_no_model_spend(
        self, deps: GraphDeps, llm: FakeLLM
    ) -> None:
        deps.scorer = FakeScorer(uplift=-0.4)
        with session_scope() as s:
            entity_id = make_entity(s)
        result = run(entity_id, deps)
        assert result["route"].route == "no_action" and llm.calls == []

    def test_fraud_signature_diverts_without_asking_the_model(
        self, deps: GraphDeps, llm: FakeLLM
    ) -> None:
        with session_scope() as s:
            entity_id = make_entity(s)
            for _ in range(3):
                link(s, entity_id, make_entity(s), EdgeType.SHARED_PHONE)
        result = run(entity_id, deps)

        assert result["priority"].fraud_band == "signature" and result["priority"].divert
        assert result["route"].route == "diverted"
        assert llm.calls == []
        with session_scope() as s:
            row = s.scalar(select(Case).where(Case.entity_id == entity_id))
            assert row is not None and row.disposition == CaseDisposition.DIVERTED

    def test_ambiguous_fraud_band_asks_the_model_and_obeys_a_keep(
        self, deps: GraphDeps, llm: FakeLLM
    ) -> None:
        with session_scope() as s:
            entity_id = make_entity(s)
            link(s, entity_id, make_entity(s), EdgeType.SHARED_ADDRESS)
            link(s, entity_id, make_entity(s, n_delinquent=0), EdgeType.SHARED_ADDRESS)
        result = run(entity_id, deps)

        assert result["priority"].fraud_band == "ambiguous" and result["priority"].llm_used
        assert llm.nodes_called()[0] == "prioritise"
        assert result["route"].route == "ready_for_dispatch"

    def test_model_outage_in_the_ambiguous_band_forces_human_review_not_a_send(
        self, deps: GraphDeps
    ) -> None:
        deps.llm = FakeLLM({"prioritise": RuntimeError("gateway down")})
        with session_scope() as s:
            entity_id = make_entity(s)
            link(s, entity_id, make_entity(s), EdgeType.SHARED_ADDRESS)
            link(s, entity_id, make_entity(s, n_delinquent=0), EdgeType.SHARED_ADDRESS)
        result = run(entity_id, deps)

        assert result["priority"].needs_review
        assert result["guard"].verdict is Verdict.ESCALATE
        assert interrupted(result)

    def test_model_failures_in_strategy_and_draft_degrade_safely(self, deps: GraphDeps) -> None:
        deps.llm = FakeLLM({"strategy": RuntimeError("x"), "draft": RuntimeError("x")})
        with session_scope() as s:
            entity_id = make_entity(s)
        result = run(entity_id, deps)
        assert result["strategy"].llm_used is False and result["draft"].llm_used is False
        assert result["route"].route == "ready_for_dispatch"  # fallback template passes the rules


class TestHumanReview:
    def _high_exposure(self) -> uuid.UUID:
        with session_scope() as s:
            return make_entity(s, overdue="6000.00")

    def test_high_exposure_interrupts_and_a_human_can_approve(self, deps: GraphDeps) -> None:
        entity_id = self._high_exposure()
        saver = InMemorySaver()
        parked = run(entity_id, deps, saver)
        assert (interrupted(parked) and "route" not in parked) or parked.get("route") is None

        done = resume_case(
            entity_id, ReviewDecision(decision="approve", reviewer="pat"), deps, saver, AS_OF
        )
        assert done["route"].route == "ready_for_dispatch"
        assert done["guard"].human_override is True and done["route"].reviewer == "pat"

    def test_a_human_can_reject_an_escalation(self, deps: GraphDeps) -> None:
        entity_id = self._high_exposure()
        saver = InMemorySaver()
        run(entity_id, deps, saver)
        done = resume_case(
            entity_id, ReviewDecision(decision="reject", reviewer="pat"), deps, saver, AS_OF
        )
        assert done["route"].route == "rejected_by_reviewer"

    def test_resume_does_not_replay_earlier_nodes_or_model_calls(
        self, deps: GraphDeps, llm: FakeLLM
    ) -> None:
        entity_id = self._high_exposure()
        saver = InMemorySaver()
        run(entity_id, deps, saver)
        calls_before = len(llm.calls)
        resume_case(
            entity_id, ReviewDecision(decision="approve", reviewer="pat"), deps, saver, AS_OF
        )
        assert len(llm.calls) == calls_before  # resumed at route; nothing re-drafted

    def test_an_unresolved_screening_is_a_hard_block_that_no_reviewer_can_approve(
        self, deps: GraphDeps, llm: FakeLLM
    ) -> None:
        with session_scope() as s:
            entity_id = make_entity(s, screening=None)  # never screened
        saver = InMemorySaver()
        parked = run(entity_id, deps, saver)
        assert interrupted(parked)
        assert llm.calls == []  # strategy/draft skipped: the guard was going to block anyway

        done = resume_case(
            entity_id, ReviewDecision(decision="approve", reviewer="pat"), deps, saver, AS_OF
        )
        assert done["route"].route == "blocked"
        assert done["guard"].verdict is Verdict.BLOCK and done["guard"].human_override is False
        with session_scope() as s:
            row = s.scalar(select(Case).where(Case.entity_id == entity_id))
            assert row is not None and row.disposition == CaseDisposition.ESCALATED

    def test_a_sanctions_hit_is_blocked(self, deps: GraphDeps) -> None:
        with session_scope() as s:
            entity_id = make_entity(s, screening="hit")
        saver = InMemorySaver()
        run(entity_id, deps, saver)
        done = resume_case(
            entity_id, ReviewDecision(decision="approve", reviewer="pat"), deps, saver, AS_OF
        )
        assert done["route"].route == "blocked"


class TestObservability:
    def test_every_executed_node_emits_a_span_with_tokens_and_cost(
        self, deps: GraphDeps, spans: InMemorySpanExporter
    ) -> None:
        with session_scope() as s:
            entity_id = make_entity(s)
        run(entity_id, deps)

        finished = {sp.name: sp for sp in spans.get_finished_spans()}
        expected = ["case_assembly", "scoring", "prioritise", "strategy", "draft", "guard", "route"]
        assert set(finished) == {f"continuum.node.{n}" for n in expected}
        for name, span in finished.items():
            attrs = span.attributes or {}
            for key in (
                "gen_ai.usage.input_tokens",
                "gen_ai.usage.output_tokens",
                "continuum.llm.cost_usd",
            ):
                assert key in attrs, (name, key)
            assert "continuum.entity_hash" in attrs and str(entity_id) not in str(attrs.values())

        strategy = finished["continuum.node.strategy"].attributes or {}
        assert strategy["gen_ai.usage.input_tokens"] == 100 and strategy["continuum.llm.calls"] == 1
        assert (finished["continuum.node.guard"].attributes or {})["gen_ai.usage.input_tokens"] == 0

    def test_llm_free_nodes_report_zero_cost(
        self, deps: GraphDeps, spans: InMemorySpanExporter
    ) -> None:
        with session_scope() as s:
            entity_id = make_entity(s)
        run(entity_id, deps)
        by_name = {sp.name: sp.attributes or {} for sp in spans.get_finished_spans()}
        for node in ("case_assembly", "scoring", "guard", "route"):
            assert by_name[f"continuum.node.{node}"]["continuum.llm.cost_usd"] == 0.0
        assert by_name["continuum.node.draft"]["continuum.llm.cost_usd"] > 0.0


def test_fraud_adjudication_schema_is_what_the_prompt_asks_for() -> None:
    assert FraudAdjudication(divert=True, confidence=0.9, reasoning="x").divert
