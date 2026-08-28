"""party_record — raw customer record as received. Never mutated (CLAUDE.md §6)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from continuum.db.base import Base


class PartyRecord(Base):
    """One raw identity record as it arrived from a source system.

    A single real person may be represented by several PartyRecords (duplicate
    applications, re-financing under a slightly different name, etc.) — resolving that
    is exactly what continuum.resolution does in Phase 2. This table is append-only in
    spirit: rows are inserted by the loader and never edited afterwards.
    """

    __tablename__ = "party_record"

    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(128), nullable=False)

    full_name: Mapped[str] = mapped_column(String(256), nullable=False)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    national_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    address_line: Mapped[str | None] = mapped_column(String(256), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_party_record_source_natural_key",
            "source_system",
            "source_record_id",
            unique=True,
        ),
        # Trigram index for fuzzy name blocking/candidate generation (CLAUDE.md §6).
        Index(
            "ix_party_record_name_trgm",
            "full_name",
            postgresql_using="gin",
            postgresql_ops={"full_name": "gin_trgm_ops"},
        ),
    )
