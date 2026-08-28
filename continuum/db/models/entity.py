"""entity, entity_member, entity_edge — resolved person and one-hop network.

Populated by continuum.resolution (Phase 2). Left as empty tables in Phase 1's migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Numeric, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from continuum.db.base import Base


class EdgeType(StrEnum):
    SHARED_ADDRESS = "shared_address"
    SHARED_PHONE = "shared_phone"
    NAME_SIMILARITY = "name_similarity"


class Entity(Base):
    """A resolved person, i.e. a Splink cluster of one or more PartyRecords."""

    __tablename__ = "entity"

    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    cluster_size: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    resolution_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EntityMember(Base):
    """Cluster membership: which party_records make up this entity, and how confidently."""

    __tablename__ = "entity_member"

    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.entity_id"), primary_key=True
    )
    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("party_record.record_id"), primary_key=True
    )
    match_probability: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)


class EntityEdge(Base):
    """A one-hop network link between two entities. Materialised nightly.

    Stored directionally (entity_id_a < entity_id_b as text, enforced by the writer, not
    the schema) with a covering index on both directions so either side of the link is a
    single index lookup.
    """

    __tablename__ = "entity_edge"

    edge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_id_a: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.entity_id"), nullable=False
    )
    entity_id_b: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.entity_id"), nullable=False
    )
    edge_type: Mapped[EdgeType] = mapped_column(
        Enum(EdgeType, name="edge_type", native_enum=False, length=32), nullable=False
    )
    strength: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    materialised_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_entity_edge_a", "entity_id_a"),
        Index("ix_entity_edge_b", "entity_id_b"),
    )
