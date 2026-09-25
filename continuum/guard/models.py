"""Guard contracts: what the guard is shown, and what it decides.

CLAUDE.md I2: the guard is the single egress point — nothing is dispatched without one of
these verdicts. The input deliberately contains only facts (screening state, amounts, times,
counts, the message text) and never a model's opinion of whether sending is fine, so a prompt
injection that changes the drafted text can still only be judged by the deterministic rules.

A verdict is *signed* (continuum.guard.signing) over the verdict, the entity, channel, send
time and the SHA-256 of the exact message text. Dispatch verifies the signature, so a verdict
cannot be fabricated, edited (e.g. BLOCK -> PASS, or a human_override added) or reused for a
different message.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from continuum.screening.status import ScreeningState


class Verdict(StrEnum):
    PASS = "pass"
    #: A human may approve sending (e.g. high exposure).
    ESCALATE = "escalate"
    #: Final. No human override can turn this into a send (e.g. sanctions hit).
    BLOCK = "block"


class RuleOutcome(StrEnum):
    PASS = "pass"
    ESCALATE = "escalate"
    BLOCK = "block"


class RuleResult(BaseModel):
    rule_id: str
    outcome: RuleOutcome
    detail: str


class ToneResult(BaseModel):
    """Outcome of the LLM tone check. `checked=False` means it could not run (fail closed)."""

    checked: bool
    acceptable: bool
    issues: list[str] = Field(default_factory=list)
    reason: str = ""
    # Model usage, for the guard node's span. Not part of the signed payload.
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class GuardInput(BaseModel):
    """Everything the deterministic rules need. Built by the graph's guard node."""

    entity_id: uuid.UUID
    case_id: uuid.UUID | None = None
    screening: ScreeningState
    total_overdue: float
    channel: str | None  # "sms" | "email" | None if nothing was chosen
    has_phone: bool
    has_email: bool
    send_at: datetime | None
    recent_contacts_7d: int
    message_body: str
    upstream_flags: list[str] = Field(default_factory=list)  # reasons upstream wants review


class OutboundMessage(BaseModel):
    """The exact message dispatch may send. Its body must hash to the verdict's message_hash."""

    entity_id: uuid.UUID
    case_id: uuid.UUID | None = None
    channel: str
    subject: str | None = None
    body: str
    send_at: datetime


class GuardVerdict(BaseModel):
    verdict: Verdict
    rules: list[RuleResult]
    entity_id: uuid.UUID
    case_id: uuid.UUID | None = None
    channel: str | None = None
    send_at: datetime | None = None
    #: SHA-256 hex of the exact message text that was evaluated.
    message_hash: str
    issued_at: datetime
    tone: ToneResult | None = None
    #: Set only by continuum.guard.engine.apply_human_approval, never possible for BLOCK.
    human_override: bool = False
    approved_by: str | None = None
    #: HMAC over the fields above; set by the engine, checked by dispatch.
    signature: str = ""

    @property
    def reasons(self) -> list[str]:
        return [f"{r.rule_id}: {r.detail}" for r in self.rules if r.outcome != RuleOutcome.PASS]

    @property
    def permits_dispatch(self) -> bool:
        """PASS, or ESCALATE that a human approved. Never BLOCK."""
        if self.verdict is Verdict.PASS:
            return True
        return self.verdict is Verdict.ESCALATE and self.human_override
