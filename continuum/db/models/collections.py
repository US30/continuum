"""case, contact_event.

Deviation from CLAUDE.md §6, flagged to the user and approved in the Phase 0/1 scaffold plan:
contact_event.case_id is nullable and entity_id/account_id/source are added. Reason: Phase 1
simulates a *historical* rules-based contact policy (contact at 30 DPD weighted by exposure,
per §8) to give the Phase 4 uplift models a treatment variable, and that history predates
cases, which do not exist until Phase 5. `source` distinguishes the two populations so the
guard's frequency-cap query (Phase 6) and the eval harness can each select the rows they mean.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from continuum.db.base import Base


class ContactChannel(StrEnum):
    SMS = "sms"
    EMAIL = "email"


class ContactSource(StrEnum):
    SIMULATED_HISTORY = "simulated_history"  # Phase 1 contact-policy simulation
    RUNTIME = "runtime"  # Phase 6 live dispatch


class CaseDisposition(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    DIVERTED = "diverted"  # network fraud signature — routed out of collections (§7)


class Case(Base):
    """Spans one or more accounts for a single entity (CLAUDE.md §6). Assembled in Phase 5."""

    __tablename__ = "case"

    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.entity_id"), nullable=False
    )
    rank: Mapped[int | None] = mapped_column(nullable=True)
    expected_recovery: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    disposition: Mapped[CaseDisposition] = mapped_column(
        Enum(CaseDisposition, name="case_disposition", native_enum=False, length=16),
        nullable=False,
        default=CaseDisposition.OPEN,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_case_entity_id", "entity_id"),)


class ContactEvent(Base):
    """Channel, timestamp, body hash, guard verdict, response. Enforces frequency caps."""

    __tablename__ = "contact_event"

    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Nullable: simulated-history rows (Phase 1) predate case assembly (Phase 5).
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("case.case_id"), nullable=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.entity_id"), nullable=False
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.account_id"), nullable=True
    )
    source: Mapped[ContactSource] = mapped_column(
        Enum(ContactSource, name="contact_source", native_enum=False, length=32),
        nullable=False,
    )
    channel: Mapped[ContactChannel] = mapped_column(
        Enum(ContactChannel, name="contact_channel", native_enum=False, length=8),
        nullable=False,
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    guard_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    responded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Frequency-cap query shape used by both the historical simulation and the
        # Phase 6 guard: "how many contacts has this entity had in the last N days".
        Index("ix_contact_event_entity_sent_at", "entity_id", "sent_at"),
    )
