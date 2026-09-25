"""Guard contracts: what the guard is shown, and what it decides.

CLAUDE.md I2: the guard is the single egress point — nothing is dispatched without one of
these verdicts. The input deliberately contains only facts (screening state, amounts, times,
counts, the message text) and never a model's opinion of whether sending is fine, so a prompt
injection that changes the drafted text can still only be judged by the deterministic rules.
"""

from __future__ import annotations

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


class GuardInput(BaseModel):
    """Everything the deterministic rules need. Built by the graph's guard node."""

    screening: ScreeningState
    total_overdue: float
    channel: str | None  # "sms" | "email" | None if nothing was chosen
    has_phone: bool
    has_email: bool
    send_at: datetime | None
    recent_contacts_7d: int
    message_body: str
    upstream_flags: list[str] = Field(default_factory=list)  # reasons upstream wants review


class GuardVerdict(BaseModel):
    verdict: Verdict
    rules: list[RuleResult]
    #: Set by the graph when a human approved an ESCALATE verdict. Never possible for BLOCK.
    human_override: bool = False

    @property
    def reasons(self) -> list[str]:
        return [f"{r.rule_id}: {r.detail}" for r in self.rules if r.outcome != RuleOutcome.PASS]
