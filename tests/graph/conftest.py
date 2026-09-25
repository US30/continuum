"""Fixtures for decisioning-graph tests: a fake LLM, a fake scorer, and row builders.

Graph logic is tested against the real (isolated test) Postgres because case assembly is SQL;
only the two external dependencies — the model and the trained scorer — are faked, through the
same GraphDeps seam production uses.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel
from sqlalchemy.orm import Session

from continuum.config.models import LogicalNode
from continuum.config.settings import get_settings
from continuum.db.models.account import Account, DelinquencyBucket, PaymentEvent
from continuum.db.models.entity import EdgeType, Entity, EntityEdge, EntityMember
from continuum.db.models.party import PartyRecord
from continuum.db.models.screening import SanctionsEntity, ScreeningDecision, ScreeningMethod
from continuum.db.session import session_scope
from continuum.graph.deps import GraphDeps
from continuum.guard.models import ToneResult
from continuum.llm.client import LLMResult, LLMUsage
from continuum.scoring.runtime import AccountScore
from continuum.telemetry.spans import configure_tracing

AS_OF = date(2026, 1, 1)
SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-00000000aaaa")
RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class FakeLLM:
    """Returns canned structured outputs per logical node and records every call."""

    def __init__(self, responses: dict[str, Callable[[str], BaseModel] | Exception] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []

    def complete[T: BaseModel](
        self, node: LogicalNode, system_prompt: str, user_prompt: str, response_model: type[T]
    ) -> LLMResult[T]:
        self.calls.append((node, user_prompt))
        response = self.responses.get(node)
        if isinstance(response, Exception):
            raise response
        parsed = response(user_prompt) if response else _default(node, response_model)
        assert isinstance(parsed, response_model)
        usage = LLMUsage(
            model="fake/model", calls=1, input_tokens=100, output_tokens=20, cost_usd=0.0001
        )
        return LLMResult(parsed=parsed, usage=usage)

    def nodes_called(self) -> list[str]:
        return [n for n, _ in self.calls]


def _default(node: str, model: type[BaseModel]) -> BaseModel:
    from continuum.graph.schemas import DraftOutput, FraudAdjudication, StrategyChoice

    if model is DraftOutput:
        return DraftOutput(
            body="Hi Sam, your payment is overdue. Please get in touch to talk options."
        )
    if model is StrategyChoice:
        return StrategyChoice(option_index=0, rationale="first option")
    if model is FraudAdjudication:
        return FraudAdjudication(divert=False, confidence=0.6, reasoning="looks like a household")
    raise AssertionError(f"no default for {node}")


class FakeTone:
    """Tone checker double: acceptable by default, records what it was asked to review."""

    def __init__(self, acceptable: bool = True, checked: bool = True) -> None:
        self.acceptable, self.checked = acceptable, checked
        self.messages: list[str] = []

    def check(self, message: str, channel: str) -> ToneResult:
        self.messages.append(message)
        return ToneResult(
            checked=self.checked,
            acceptable=self.acceptable,
            issues=[] if self.acceptable else ["too forceful"],
            reason="" if self.checked else "tone check unavailable: test",
            model="fake/tone",
            input_tokens=50,
            output_tokens=10,
            cost_usd=0.00005,
        )


class FakeScorer:
    def __init__(self, uplift: float = 0.3) -> None:
        self.uplift = uplift

    def score(self, account_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, AccountScore]:
        return {
            a: AccountScore(0.3, 0.3 + self.uplift, self.uplift, overdue_amount=200.0)
            for a in account_ids
        }


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def plain_deps(llm: FakeLLM) -> GraphDeps:
    """Deps for pure-logic tests: never touches the database."""
    return GraphDeps(
        session_scope=session_scope,
        scorer=FakeScorer(),
        llm=llm,
        tone=FakeTone(),
        settings=get_settings(),
    )


@pytest.fixture
def deps(llm: FakeLLM, _one_sanctions_snapshot: None) -> GraphDeps:
    return GraphDeps(
        session_scope=session_scope,
        scorer=FakeScorer(),
        llm=llm,
        tone=FakeTone(),
        settings=get_settings(),
    )


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = configure_tracing([SimpleSpanProcessor(exporter)])
    yield exporter
    provider.shutdown()


@pytest.fixture(scope="session")
def _one_sanctions_snapshot(_test_database: None) -> None:
    """screening_state reads the current snapshot; keep exactly one in the test database."""
    with session_scope() as session:
        if session.get(SanctionsEntity, "graph-test-listing") is None:
            session.add(
                SanctionsEntity(
                    listing_id="graph-test-listing",
                    snapshot_id=SNAPSHOT_ID,
                    primary_name="Nobody Listed",
                    schema_type="Person",
                    is_current=True,
                )
            )


def make_entity(
    session: Session,
    *,
    n_delinquent: int = 1,
    overdue: str = "200.00",
    screening: str | None = "clear",
    phone: str | None = "555-0100",
    email: str | None = "sam@example.test",
    full_name: str = "Sam Tester",
) -> uuid.UUID:
    """One entity with `n_delinquent` DPD_30 accounts, each with one unpaid installment."""
    entity_id, record_id = uuid.uuid4(), uuid.uuid4()
    session.add(Entity(entity_id=entity_id, cluster_size=1, confidence=1, resolution_run_id=RUN_ID))
    session.add(
        PartyRecord(
            record_id=record_id,
            source_system="loan_origination",
            source_record_id=str(record_id),
            full_name=full_name,
            date_of_birth=date(1980, 1, 1),
            phone=phone,
            email=email,
        )
    )
    session.flush()
    session.add(EntityMember(entity_id=entity_id, record_id=record_id, match_probability=1))
    for _ in range(n_delinquent):
        account_id = uuid.uuid4()
        session.add(
            Account(
                account_id=account_id,
                entity_id=entity_id,
                product="personal_loan",
                exposure=Decimal("3000.00"),
                origination_date=date(2025, 6, 1),
                current_state=DelinquencyBucket.DPD_30,
            )
        )
        session.flush()
        session.add(
            PaymentEvent(
                event_id=uuid.uuid4(),
                account_id=account_id,
                due_date=date(2025, 11, 20),
                paid_date=None,
                amount_due=Decimal(overdue),
                amount_paid=Decimal("0"),
            )
        )
    if screening is not None:
        session.add(
            ScreeningDecision(
                decision_id=uuid.uuid4(),
                entity_id=entity_id,
                snapshot_id=SNAPSHOT_ID,
                method=ScreeningMethod.AUTO_REJECT,
                outcome=screening,
            )
        )
    session.flush()
    return entity_id


def link(session: Session, a: uuid.UUID, b: uuid.UUID, edge_type: EdgeType) -> None:
    session.add(
        EntityEdge(
            edge_id=uuid.uuid4(),
            entity_id_a=a,
            entity_id_b=b,
            edge_type=edge_type,
            strength=1,
        )
    )
    session.flush()


def node_names(result: dict[str, Any]) -> list[str]:
    return [e.split(":")[0] for e in result["events"]]
