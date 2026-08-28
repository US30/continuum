"""Table-driven tests for the pure SCD2 delinquency state machine.

CLAUDE.md §10: "Tests before implementation for deterministic modules... these have
exact expected outputs; write them first."
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from continuum.db.models.account import DelinquencyBucket
from continuum.foundation.delinquency import (
    CHARGE_OFF_DAYS,
    PaymentRecord,
    StateInterval,
    derive_delinquency_states,
)

ORIGINATION = date(2025, 1, 1)


def paid(due: date, amount: str = "100") -> PaymentRecord:
    return PaymentRecord(
        due_date=due, paid_date=due, amount_due=Decimal(amount), amount_paid=Decimal(amount)
    )


def unpaid(due: date, amount: str = "100") -> PaymentRecord:
    return PaymentRecord(
        due_date=due, paid_date=None, amount_due=Decimal(amount), amount_paid=Decimal("0")
    )


def paid_late(due: date, paid_on: date, amount: str = "100") -> PaymentRecord:
    return PaymentRecord(
        due_date=due, paid_date=paid_on, amount_due=Decimal(amount), amount_paid=Decimal(amount)
    )


def assert_scd2_shape(intervals: list[StateInterval]) -> None:
    """The invariant every result must satisfy regardless of the scenario: intervals are
    contiguous, non-overlapping, and exactly one is open (effective_to is None).
    """
    open_rows = [i for i in intervals if i.effective_to is None]
    assert len(open_rows) == 1
    for earlier, later in zip(intervals, intervals[1:], strict=True):
        assert earlier.effective_to == later.effective_from


class TestCleanPayer:
    def test_all_paid_on_time_stays_current(self) -> None:
        payments = [paid(date(2025, 1, 1)), paid(date(2025, 2, 1)), paid(date(2025, 3, 1))]
        intervals = derive_delinquency_states(payments, ORIGINATION, as_of=date(2025, 3, 15))

        assert_scd2_shape(intervals)
        assert len(intervals) == 1
        assert intervals[0].state == DelinquencyBucket.CURRENT
        assert intervals[0].days_past_due == 0

    def test_payment_on_due_date_boundary_is_current(self) -> None:
        due = date(2025, 1, 1)
        intervals = derive_delinquency_states([paid(due)], ORIGINATION, as_of=due)
        assert_scd2_shape(intervals)
        assert intervals[-1].state == DelinquencyBucket.CURRENT


class TestRollForward:
    def test_missing_installment_rolls_30_60_90(self) -> None:
        due = date(2025, 1, 1)
        payments = [unpaid(due)]

        at_30 = derive_delinquency_states(payments, ORIGINATION, as_of=due.replace(day=31))
        assert_scd2_shape(at_30)
        assert at_30[-1].state == DelinquencyBucket.DPD_30

        as_of_90 = date(2025, 4, 1)  # 90 days after due
        at_90 = derive_delinquency_states(payments, ORIGINATION, as_of=as_of_90)
        assert_scd2_shape(at_90)
        assert at_90[-1].state == DelinquencyBucket.DPD_90
        # Must have passed through 30 and 60 on the way, in order.
        states_in_order = [i.state for i in at_90]
        assert states_in_order == [
            DelinquencyBucket.CURRENT,
            DelinquencyBucket.DPD_30,
            DelinquencyBucket.DPD_60,
            DelinquencyBucket.DPD_90,
        ]

    def test_charge_off_is_terminal(self) -> None:
        due = date(2025, 1, 1)
        payments = [unpaid(due)]
        as_of = due + timedelta(days=CHARGE_OFF_DAYS + 30)

        intervals = derive_delinquency_states(payments, ORIGINATION, as_of=as_of)
        assert intervals[-1].state == DelinquencyBucket.CHARGED_OFF
        assert intervals[-1].effective_to is None
        # No interval after charge-off.
        assert sum(1 for i in intervals if i.state == DelinquencyBucket.CHARGED_OFF) == 1


class TestCure:
    def test_cure_at_30_dpd(self) -> None:
        due = date(2025, 1, 1)
        # Paid 40 days late: crosses into 30 DPD, then pays before reaching 60.
        payments = [paid_late(due, date(2025, 2, 10))]
        intervals = derive_delinquency_states(payments, ORIGINATION, as_of=date(2025, 2, 10))

        assert_scd2_shape(intervals)
        assert intervals[-1].state == DelinquencyBucket.CURED
        assert intervals[-1].days_past_due == 0
        assert any(i.state == DelinquencyBucket.DPD_30 for i in intervals)

    def test_cure_at_60_dpd(self) -> None:
        due = date(2025, 1, 1)
        payments = [paid_late(due, date(2025, 3, 15))]  # ~73 days late -> reached 60 first
        intervals = derive_delinquency_states(payments, ORIGINATION, as_of=date(2025, 3, 15))

        assert_scd2_shape(intervals)
        assert intervals[-1].state == DelinquencyBucket.CURED
        assert any(i.state == DelinquencyBucket.DPD_60 for i in intervals)


class TestMultiInstallment:
    def test_oldest_unpaid_installment_drives_dpd(self) -> None:
        # First installment unpaid, second paid on time — DPD must be measured from the
        # oldest unpaid due date, not reset by the later on-time payment.
        payments = [unpaid(date(2025, 1, 1)), paid(date(2025, 2, 1))]
        intervals = derive_delinquency_states(payments, ORIGINATION, as_of=date(2025, 1, 31))
        assert_scd2_shape(intervals)
        assert intervals[-1].state == DelinquencyBucket.DPD_30

    def test_out_of_order_input_list_gives_same_result(self) -> None:
        ordered = [unpaid(date(2025, 1, 1)), paid(date(2025, 2, 1))]
        shuffled = [paid(date(2025, 2, 1)), unpaid(date(2025, 1, 1))]

        as_of = date(2025, 1, 31)
        assert derive_delinquency_states(ordered, ORIGINATION, as_of) == derive_delinquency_states(
            shuffled, ORIGINATION, as_of
        )


class TestEmpty:
    def test_no_payment_events_yet_is_current(self) -> None:
        intervals = derive_delinquency_states([], ORIGINATION, as_of=ORIGINATION)
        assert len(intervals) == 1
        assert intervals[0].state == DelinquencyBucket.CURRENT
        assert intervals[0].effective_to is None
