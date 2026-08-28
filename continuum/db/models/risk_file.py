"""risk_file_event — append-only. CLAUDE.md I3: never UPDATE or DELETE.

Current state is a view over the latest event per entity (see the partial index below and
the (future) `current_risk_file` view created alongside it). No ORM method on this model
performs an update; continuum.db.session's normal session.merge()/update() should never be
called against RiskFileEvent — enforced by convention here and by a test in Phase 3+ once
the writer exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from continuum.db.base import Base


class RiskFileEventType(StrEnum):
    SCREENING_OUTCOME = "screening_outcome"
    DELINQUENCY_TRANSITION = "delinquency_transition"
    NETWORK_UPDATE = "network_update"
    CASE_DISPOSITION = "case_disposition"


class RiskFileEvent(Base):
    """One append-only fact about an entity's risk file. See CLAUDE.md I3."""

    __tablename__ = "risk_file_event"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.entity_id"), nullable=False
    )
    event_type: Mapped[RiskFileEventType] = mapped_column(
        Enum(RiskFileEventType, name="risk_file_event_type", native_enum=False, length=32),
        nullable=False,
    )
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # JSON-encoded, type-specific
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_risk_file_event_entity_recorded", "entity_id", "recorded_at"),
        # The guard's hot-path index (CLAUDE.md §6): fast lookup of the latest screening
        # outcome for an entity, without scanning every event type.
        Index(
            "ix_risk_file_event_screening_outcome",
            "entity_id",
            "recorded_at",
            postgresql_where=(event_type == RiskFileEventType.SCREENING_OUTCOME),
        ),
    )
