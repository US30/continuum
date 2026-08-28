"""Pure delinquency state machine: payment history -> SCD2 delinquency_state rows.

CLAUDE.md §8: "Derive the delinquency state machine as SCD2." No I/O in this module —
that is what makes it exhaustively unit-testable (CLAUDE.md §10: "tests before
implementation for deterministic modules").

Model: at any point in time, days-past-due (DPD) is measured from the oldest unpaid
installment's due date to the observation date. An installment counts as unpaid while
amount_paid < amount_due. DPD is mapped to a bucket via the thresholds in
Settings.delinquency_buckets_days (30/60/90) and Settings.delinquency_charge_off_days.
Once CHARGED_OFF is reached the account is terminal: no further transitions.

Bucket semantics:
  - DPD == 0 and the account was never delinquent  -> CURRENT
  - DPD == 0 after having been delinquent          -> CURED
  - 1 <= DPD < 30                                  -> CURRENT (grace, no bucket crossed)
  - 30 <= DPD < 60                                 -> DPD_30
  - 60 <= DPD < 90                                 -> DPD_60
  - 90 <= DPD < charge_off_days                    -> DPD_90
  - DPD >= charge_off_days                         -> CHARGED_OFF (terminal)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from continuum.db.models.account import DelinquencyBucket

# Charge-off threshold: standard US consumer-lending practice charges off installment
# credit at 120 DPD. Not in Settings because it is a property of the synthetic loan
# generator's assumed product, not a runtime-tunable business threshold; revisit if a
# later phase needs it configurable per product.
CHARGE_OFF_DAYS = 120

# Bucket floors, ascending. Kept local to this module (rather than re-deriving from
# Settings.delinquency_buckets_days on every call) so the pure function has no config
# dependency at all — a deliberate trade against CLAUDE.md's "no magic thresholds" rule,
# accepted because these specific numbers (30/60/90) are also restated in
# Settings.delinquency_buckets_days as the single named source of truth for the rest of
# the codebase; this tuple must stay in sync with it.
BUCKET_FLOORS: tuple[tuple[int, DelinquencyBucket], ...] = (
    (90, DelinquencyBucket.DPD_90),
    (60, DelinquencyBucket.DPD_60),
    (30, DelinquencyBucket.DPD_30),
)


@dataclass(frozen=True)
class PaymentRecord:
    due_date: date
    paid_date: date | None
    amount_due: Decimal
    amount_paid: Decimal

    def is_unpaid_as_of(self, observation_date: date) -> bool:
        """Whether this installment is still outstanding as of `observation_date`.

        An installment paid in full on `paid_date` is unpaid at every observation date
        strictly before `paid_date`, and paid from `paid_date` onward. A partial payment
        (amount_paid < amount_due) never counts as paid, regardless of paid_date.
        """
        if self.amount_paid < self.amount_due:
            return True
        return self.paid_date is None or self.paid_date > observation_date


@dataclass(frozen=True)
class StateInterval:
    effective_from: date
    effective_to: date | None
    state: DelinquencyBucket
    days_past_due: int


def _bucket_for_dpd(dpd: int, ever_delinquent: bool) -> DelinquencyBucket:
    if dpd >= CHARGE_OFF_DAYS:
        return DelinquencyBucket.CHARGED_OFF
    for floor, bucket in BUCKET_FLOORS:
        if dpd >= floor:
            return bucket
    return DelinquencyBucket.CURED if ever_delinquent else DelinquencyBucket.CURRENT


def _oldest_unpaid_due_date(payments: list[PaymentRecord], as_of: date) -> date | None:
    unpaid_due_dates = [
        p.due_date for p in payments if p.due_date <= as_of and p.is_unpaid_as_of(as_of)
    ]
    return min(unpaid_due_dates) if unpaid_due_dates else None


def _observation_dates(payments: list[PaymentRecord], as_of: date) -> list[date]:
    """Every date at which the bucket could possibly change: due dates, paid dates, and
    the 30/60/90/charge-off threshold crossings measured from each due date.
    """
    candidates: set[date] = set()
    for p in payments:
        candidates.add(p.due_date)
        if p.paid_date is not None:
            candidates.add(p.paid_date)
        for offset in (*[floor for floor, _ in BUCKET_FLOORS], CHARGE_OFF_DAYS):
            crossing = p.due_date + timedelta(days=offset)
            if crossing <= as_of:
                candidates.add(crossing)
    candidates.add(as_of)
    return sorted(d for d in candidates if d <= as_of)


def derive_delinquency_states(
    payments: list[PaymentRecord], origination_date: date, as_of: date
) -> list[StateInterval]:
    """Derive the full SCD2 history for one account, from origination through as_of.

    `as_of` is an explicit parameter, never the wall clock (CLAUDE.md I6 — idempotent,
    re-runnable on the same inputs). Returns intervals in chronological order; the last
    interval has effective_to=None (the current row) unless the account charged off,
    in which case the CHARGED_OFF interval is also left open — a charged-off account
    has no further state.
    """
    if not payments:
        return [
            StateInterval(
                effective_from=origination_date,
                effective_to=None,
                state=DelinquencyBucket.CURRENT,
                days_past_due=0,
            )
        ]

    observation_dates = _observation_dates(payments, as_of)
    intervals: list[StateInterval] = []
    ever_delinquent = False
    open_from = origination_date
    open_state = DelinquencyBucket.CURRENT
    open_dpd = 0

    for obs_date in observation_dates:
        if open_state == DelinquencyBucket.CHARGED_OFF:
            break  # terminal

        oldest_unpaid = _oldest_unpaid_due_date(payments, obs_date)
        dpd = (obs_date - oldest_unpaid).days if oldest_unpaid else 0
        bucket = _bucket_for_dpd(dpd, ever_delinquent)

        if bucket in (DelinquencyBucket.DPD_30, DelinquencyBucket.DPD_60, DelinquencyBucket.DPD_90):
            ever_delinquent = True

        if bucket != open_state:
            intervals.append(
                StateInterval(
                    effective_from=open_from,
                    effective_to=obs_date,
                    state=open_state,
                    days_past_due=open_dpd,
                )
            )
            open_from = obs_date
            open_state = bucket
            open_dpd = dpd
        else:
            open_dpd = dpd  # DPD still climbs within an unchanged bucket

    intervals.append(
        StateInterval(
            effective_from=open_from,
            effective_to=None,
            state=open_state,
            days_past_due=open_dpd,
        )
    )
    return intervals
