"""ground_truth_link — planted collisions. EVAL-ONLY. CLAUDE.md I4.

"ground_truth_link is invisible to runtime code. Only the evaluation harness may read it.
A runtime import of it is a build failure."

This module (and this table) may be imported by:
  - continuum.foundation.collisions (the only writer — it plants the collisions)
  - eval/ (the only reader — resolution precision/recall against ground truth)

Nothing else. tests/test_invariants.py greps continuum/ for imports of this module from
outside those two locations and fails the build if found.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from continuum.db.base import Base


class CollisionType(StrEnum):
    SAME_PERSON = "same_person"  # true positive: both records are the same real person
    HARD_NEGATIVE = "hard_negative"  # near-identical but genuinely different people


class GroundTruthLink(Base):
    __tablename__ = "ground_truth_link"

    link_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    record_id_a: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("party_record.record_id"), nullable=False
    )
    record_id_b: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("party_record.record_id"), nullable=False
    )
    collision_type: Mapped[CollisionType] = mapped_column(
        Enum(CollisionType, name="collision_type", native_enum=False, length=16),
        nullable=False,
    )
    planted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
