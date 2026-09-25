"""LangGraph state schema for one collections case.

CLAUDE.md §10: Pydantic v2 models for all inter-node contracts. The state is the only thing
that flows between nodes, and it is what the Redis checkpointer persists after every node, so
it must stay JSON-serialisable and must not hold live objects (sessions, clients).

The guard's own contracts (GuardVerdict) live in continuum.guard.models and are imported here,
never the other way round — the guard must not depend on the graph.
"""

from __future__ import annotations

import operator
import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from continuum.guard.models import GuardVerdict, OutboundMessage
from continuum.screening.status import ScreeningState

FraudBand = Literal["clear", "ambiguous", "signature"]
RouteName = Literal[
    "ready_for_dispatch",  # guard PASS (or a human approved an ESCALATE): Phase 6 may send
    "blocked",  # guard BLOCK: never sent; queued for human follow-up
    "diverted",  # network fraud signature: leaves collections entirely
    "no_action",  # expected net recovery <= 0: contacting is not worth it (or hurts)
    "rejected_by_reviewer",  # a human declined an ESCALATE
    "no_delinquency",  # nothing to collect for this entity
]


class AccountView(BaseModel):
    account_id: uuid.UUID
    product: str
    exposure: float
    state: str
    days_past_due: int
    overdue_amount: float
    p_cure_no_contact: float | None = None
    p_cure_contact: float | None = None
    uplift: float | None = None

    @property
    def expected_incremental_recovery(self) -> float:
        return (self.uplift or 0.0) * self.overdue_amount


class LinkedEntity(BaseModel):
    entity_id: uuid.UUID
    edge_type: str  # shared_address | shared_phone | name_similarity
    strength: float
    delinquent: bool


class ContactPoints(BaseModel):
    first_name: str
    has_phone: bool
    has_email: bool


class CaseView(BaseModel):
    """The entity view — every delinquent account of one resolved person in ONE case.

    This is the thesis (CLAUDE.md §1): five delinquent accounts controlled by one person are
    one case with one outreach, not five parallel dunning sequences.
    """

    entity_id: uuid.UUID
    case_id: uuid.UUID
    as_of: date
    accounts: list[AccountView]
    linked_entities: list[LinkedEntity]
    screening: ScreeningState
    contact: ContactPoints
    recent_contacts_7d: int

    @property
    def total_overdue(self) -> float:
        return sum(a.overdue_amount for a in self.accounts)

    @property
    def max_days_past_due(self) -> int:
        return max((a.days_past_due for a in self.accounts), default=0)


class Priority(BaseModel):
    expected_incremental_recovery: float
    contact_cost: float
    expected_net_recovery: float
    fraud_score: float
    fraud_band: FraudBand
    divert: bool
    #: Set when an LLM step was needed but failed: a human must look before anything is sent.
    needs_review: bool = False
    rationale: str
    llm_used: bool = False

    @property
    def worth_contacting(self) -> bool:
        return self.expected_net_recovery > 0


class SendOption(BaseModel):
    channel: str
    send_at: datetime


class Strategy(BaseModel):
    options: list[SendOption]
    chosen: SendOption | None
    rationale: str
    llm_used: bool = False


class Draft(BaseModel):
    channel: str
    subject: str | None
    body: str
    prompt_version: str
    llm_used: bool = False


class Route(BaseModel):
    route: RouteName
    disposition: str
    reviewer: str | None = None
    note: str = ""


class DispatchRequest(BaseModel):
    """What the worker layer forwards to dispatch after a case clears. The graph only builds
    this; it never sends (CLAUDE.md I2). Dispatch re-verifies the signed verdict itself."""

    verdict: GuardVerdict
    message: OutboundMessage


class CaseState(BaseModel):
    entity_id: uuid.UUID
    as_of: date
    #: Groups every node span/log/checkpoint of one run; also the checkpoint thread id suffix.
    run_id: str
    case: CaseView | None = None
    priority: Priority | None = None
    strategy: Strategy | None = None
    draft: Draft | None = None
    guard: GuardVerdict | None = None
    route: Route | None = None
    dispatch: DispatchRequest | None = None
    events: Annotated[list[str], operator.add] = Field(default_factory=list)
