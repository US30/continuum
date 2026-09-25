"""account, payment_event, delinquency_state.

delinquency_state is SCD2 (CLAUDE.md §6): each row is valid for [effective_from, effective_to),
with effective_to NULL meaning "current". Transitions here are what triggers re-screening
(CLAUDE.md §7 nightly batch, screening.select).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from continuum.db.base import Base


class DelinquencyBucket(StrEnum):
    CURRENT = "current"
    DPD_30 = "30_dpd"
    DPD_60 = "60_dpd"
    DPD_90 = "90_dpd"
    CURED = "cured"
    CHARGED_OFF = "charged_off"


class Account(Base):
    __tablename__ = "account"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.entity_id"), nullable=False
    )
    product: Mapped[str] = mapped_column(String(32), nullable=False)
    exposure: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    origination_date: Mapped[date] = mapped_column(Date, nullable=False)
    current_state: Mapped[DelinquencyBucket] = mapped_column(
        Enum(DelinquencyBucket, name="delinquency_bucket", native_enum=False, length=16),
        nullable=False,
        default=DelinquencyBucket.CURRENT,
    )

    __table_args__ = (Index("ix_account_entity_id", "entity_id"),)


class PaymentEvent(Base):
    """Installment-level history. Feeds both self-cure and response propensity models."""

    __tablename__ = "payment_event"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.account_id"), nullable=False
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    paid_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount_due: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    amount_paid: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)

    __table_args__ = (Index("ix_payment_event_account_due", "account_id", "due_date"),)


class DelinquencyState(Base):
    """SCD2 slowly-changing delinquency state. Exactly one open row (effective_to IS NULL)
    per account at any time — enforced by continuum.foundation.delinquency, not by a DB
    constraint, because Postgres has no native "at most one NULL per group" constraint
    short of a partial unique index, which is applied here.
    """

    __tablename__ = "delinquency_state"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.account_id"), primary_key=True
    )
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[DelinquencyBucket] = mapped_column(
        Enum(DelinquencyBucket, name="delinquency_bucket", native_enum=False, length=16),
        nullable=False,
    )
    days_past_due: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        # The hot-path index called out in CLAUDE.md §6: latest state per account.
        Index("ix_delinquency_state_account_effective_from", "account_id", "effective_from"),
        # Partial unique index: at most one open (effective_to IS NULL) row per account.
        # This is the DB-level enforcement of "exactly one current state per account"
        # that continuum.foundation.delinquency also guarantees at the application level.
        Index(
            "uq_delinquency_state_open_row",
            "account_id",
            unique=True,
            postgresql_where=effective_to.is_(None),
        ),
    )
