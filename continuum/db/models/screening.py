"""sanctions_entity, sanctions_alias, screening_decision.

Sanctions tables are a snapshot-scoped reference listing (CLAUDE.md §6), loaded by
continuum.foundation.sanctions via atomic swap so a reader never sees a partial snapshot.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from continuum.db.base import Base


class ScreeningMethod(StrEnum):
    AUTO_CONFIRM = "auto_confirm"
    AUTO_REJECT = "auto_reject"
    ADJUDICATED = "adjudicated"
    HUMAN = "human"


class SanctionsEntity(Base):
    """One listing from a snapshot of the reference sanctions/PEP source.

    snapshot_id identifies which atomic-swap load this row belongs to; only rows from the
    current snapshot are visible to screening (enforced by the ingest swap, not a filter
    every reader must remember to apply — see continuum.foundation.sanctions).
    """

    __tablename__ = "sanctions_entity"

    listing_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    primary_name: Mapped[str] = mapped_column(String(256), nullable=False)
    schema_type: Mapped[str] = mapped_column(String(32), nullable=False)  # e.g. "Person"
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    countries: Mapped[str | None] = mapped_column(String(256), nullable=True)
    topics: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (Index("ix_sanctions_entity_snapshot", "snapshot_id"),)


class SanctionsAlias(Base):
    """One alias or transliteration of a SanctionsEntity. One row per alias, trigram indexed
    per CLAUDE.md §6 so fuzzy candidate generation (Phase 3) can hit it directly.
    """

    __tablename__ = "sanctions_alias"

    alias_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    listing_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("sanctions_entity.listing_id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)

    __table_args__ = (
        Index("ix_sanctions_alias_listing_id", "listing_id"),
        Index(
            "ix_sanctions_alias_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )


class ScreeningDecision(Base):
    """Outcome of screening one entity against the current sanctions snapshot."""

    __tablename__ = "screening_decision"

    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.entity_id"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    method: Mapped[ScreeningMethod] = mapped_column(
        Enum(ScreeningMethod, name="screening_method", native_enum=False, length=16),
        nullable=False,
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)  # e.g. "clear", "hit"
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_screening_decision_entity_id", "entity_id"),)
