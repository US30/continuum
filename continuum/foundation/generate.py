"""Seeded synthetic loan-and-payment generator.

CLAUDE.md decision (see plan): the loan dataset is fully synthetic, generated in-process
from Settings.data_seed rather than downloaded, so the whole build is reproducible with no
external dependency (CLAUDE.md I6, I8). Emits installment-level payment history — that is
what makes the SCD2 delinquency machine (continuum.foundation.delinquency) derivable
rather than invented directly.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from continuum.foundation.delinquency import PaymentRecord

# The synthetic dataset's fixed "as of" date. Every derived delinquency state and every
# simulated contact is computed relative to this date, never the wall clock — required for
# CLAUDE.md I6 (idempotent, re-runnable on the same inputs with identical output).
AS_OF_DATE = date(2026, 1, 1)

ORIGINATION_WINDOW_START = date(2024, 1, 1)
ORIGINATION_WINDOW_END = date(2025, 10, 1)

PRODUCTS = ("personal_loan", "auto_loan", "credit_card")


class DelinquencyProfile(StrEnum):
    """Target end-state used only to steer payment generation. The real delinquency_state
    rows are always recomputed by continuum.foundation.delinquency from the payments this
    profile produces — never assigned directly, so the generator can never drift out of
    sync with the state machine it is supposed to be exercising.
    """

    CLEAN = "clean"
    DELINQUENT_30 = "delinquent_30"
    DELINQUENT_60 = "delinquent_60"
    DELINQUENT_90 = "delinquent_90"
    CURED = "cured"
    CHARGED_OFF = "charged_off"


# Target mix (CLAUDE.md §8: "a controlled delinquency mix"). Chosen to give Phase 4's
# self-cure/response models enough of every class to learn from, and to give Phase 3
# screening real delinquency-state transitions to trigger re-screening on from day one.
# Not a runtime threshold (hence not in Settings) — this describes the shape of the
# synthetic dataset, not a business rule any production code path reads.
PROFILE_WEIGHTS: dict[DelinquencyProfile, float] = {
    DelinquencyProfile.CLEAN: 0.55,
    DelinquencyProfile.DELINQUENT_30: 0.15,
    DelinquencyProfile.DELINQUENT_60: 0.08,
    DelinquencyProfile.DELINQUENT_90: 0.07,
    DelinquencyProfile.CURED: 0.10,
    DelinquencyProfile.CHARGED_OFF: 0.05,
}

# Target days-past-due at AS_OF_DATE for each profile that isn't CLEAN/CHARGED_OFF —
# chosen to land comfortably inside the matching delinquency_state bucket.
_TARGET_DPD: dict[DelinquencyProfile, int] = {
    DelinquencyProfile.DELINQUENT_30: 35,
    DelinquencyProfile.DELINQUENT_60: 65,
    DelinquencyProfile.DELINQUENT_90: 95,
    DelinquencyProfile.CURED: 20,
}


@dataclass(frozen=True)
class SyntheticAccount:
    account_id: uuid.UUID
    product: str
    exposure: Decimal
    origination_date: date
    profile: DelinquencyProfile
    payments: list[PaymentRecord]


@dataclass(frozen=True)
class SyntheticPerson:
    person_id: uuid.UUID
    accounts: list[SyntheticAccount]


def _monthly_due_dates(origination: date, as_of: date) -> list[date]:
    """Monthly installment due dates from origination through as_of, inclusive."""
    dates: list[date] = []
    cursor = origination
    while cursor <= as_of:
        dates.append(cursor)
        month = cursor.month % 12 + 1
        year = cursor.year + (1 if cursor.month == 12 else 0)
        day = min(cursor.day, 28)  # avoids month-length edge cases (Jan 31 -> Feb 31)
        cursor = date(year, month, day)
    return dates


def _generate_payments(
    origination: date,
    as_of: date,
    profile: DelinquencyProfile,
    exposure: Decimal,
    rng: random.Random,
) -> list[PaymentRecord]:
    due_dates = _monthly_due_dates(origination, as_of)
    installment = (exposure / max(len(due_dates), 1)).quantize(Decimal("0.01"))

    if profile == DelinquencyProfile.CLEAN:
        return [PaymentRecord(d, d, installment, installment) for d in due_dates]

    if profile == DelinquencyProfile.CHARGED_OFF:
        # Pays on time until six installments before as_of, then never pays again —
        # comfortably past the 120-day charge-off threshold by AS_OF_DATE.
        delinquency_start_idx = max(0, len(due_dates) - 6)
        delinquency_start = due_dates[delinquency_start_idx]
        return [
            PaymentRecord(d, d, installment, installment)
            if d < delinquency_start
            else PaymentRecord(d, None, installment, Decimal("0"))
            for d in due_dates
        ]

    # DELINQUENT_30/60/90/CURED: one installment goes unpaid at the point that lands
    # as_of at the target DPD; CURED additionally pays that installment late, just before
    # as_of, so the state machine has cured by the time the snapshot is taken.
    target_dpd = _TARGET_DPD[profile]
    ideal_trouble_date = as_of - timedelta(days=target_dpd)
    eligible = [d for d in due_dates if d <= ideal_trouble_date]
    trouble_due = max(eligible) if eligible else due_dates[0]

    is_cured = profile == DelinquencyProfile.CURED
    cure_paid_date = as_of - timedelta(days=rng.randint(0, 5)) if is_cured else None

    payments: list[PaymentRecord] = []
    for d in due_dates:
        if d < trouble_due:
            payments.append(PaymentRecord(d, d, installment, installment))
        elif d == trouble_due:
            if cure_paid_date is not None:
                payments.append(PaymentRecord(d, cure_paid_date, installment, installment))
            else:
                payments.append(PaymentRecord(d, None, installment, Decimal("0")))
        else:
            # Installments after the trouble one: on-time for CURED (it has recovered),
            # otherwise also left unpaid — but they are never the *oldest* unpaid
            # installment, so they do not affect the derived DPD.
            if profile == DelinquencyProfile.CURED:
                payments.append(PaymentRecord(d, d, installment, installment))
            else:
                payments.append(PaymentRecord(d, None, installment, Decimal("0")))
    return payments


def generate_population(n_persons: int, seed: int) -> list[SyntheticPerson]:
    """Deterministic given (n_persons, seed) — required for CLAUDE.md I6 idempotency."""
    rng = random.Random(seed)
    profiles = list(PROFILE_WEIGHTS.keys())
    weights = list(PROFILE_WEIGHTS.values())

    persons: list[SyntheticPerson] = []
    for _ in range(n_persons):
        person_id = uuid.UUID(int=rng.getrandbits(128))
        n_accounts = rng.choices([1, 2, 3], weights=[0.7, 0.25, 0.05])[0]

        accounts: list[SyntheticAccount] = []
        for _ in range(n_accounts):
            span_days = (ORIGINATION_WINDOW_END - ORIGINATION_WINDOW_START).days
            origination = ORIGINATION_WINDOW_START + timedelta(days=rng.randint(0, span_days))
            exposure = Decimal(str(round(rng.lognormvariate(8.5, 0.6), 2)))
            profile = rng.choices(profiles, weights=weights)[0]
            payments = _generate_payments(origination, AS_OF_DATE, profile, exposure, rng)
            accounts.append(
                SyntheticAccount(
                    account_id=uuid.UUID(int=rng.getrandbits(128)),
                    product=rng.choice(PRODUCTS),
                    exposure=exposure,
                    origination_date=origination,
                    profile=profile,
                    payments=payments,
                )
            )
        persons.append(SyntheticPerson(person_id=person_id, accounts=accounts))
    return persons
