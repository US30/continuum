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

# Age band, in days before AS_OF_DATE, of the one installment that goes wrong for each
# non-CLEAN profile. Bands sit strictly inside the matching delinquency bucket with a 3-day
# margin (a due day > 28 is clamped to 28, moving the date up to 3 days earlier), so the
# state machine in continuum.foundation.delinquency always derives the intended bucket:
#   DELINQUENT_30 -> [30,60)   DELINQUENT_60 -> [60,90)   DELINQUENT_90 -> [90,120)
#   CURED         -> crosses 30 DPD, then is paid late (< 60 DPD at payment)
#   CHARGED_OFF   -> past the 120-day charge-off threshold
_TROUBLE_AGE_BAND: dict[DelinquencyProfile, tuple[int, int]] = {
    DelinquencyProfile.DELINQUENT_30: (32, 56),
    DelinquencyProfile.DELINQUENT_60: (62, 86),
    DelinquencyProfile.DELINQUENT_90: (92, 116),
    DelinquencyProfile.CURED: (40, 55),
    DelinquencyProfile.CHARGED_OFF: (125, 250),
}

# How many on-time installments precede the trouble installment.
_MIN_CLEAN_MONTHS_BEFORE_TROUBLE = 1
_MAX_CLEAN_MONTHS_BEFORE_TROUBLE = 10


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


def _shift_months_back(d: date, months: int) -> date:
    """`d` minus `months` calendar months. `d.day` must be <= 28 (see _trouble_due_date)."""
    total = d.year * 12 + (d.month - 1) - months
    return date(total // 12, total % 12 + 1, d.day)


def _trouble_due_date(profile: DelinquencyProfile, as_of: date, rng: random.Random) -> date:
    """Due date of the installment that goes unpaid (or late) for a non-CLEAN profile."""
    low, high = _TROUBLE_AGE_BAND[profile]
    due = as_of - timedelta(days=rng.randint(low, high))
    return due.replace(day=min(due.day, 28))


def _generate_payments(
    origination: date,
    as_of: date,
    profile: DelinquencyProfile,
    exposure: Decimal,
    rng: random.Random,
    trouble_due: date | None = None,
) -> list[PaymentRecord]:
    due_dates = _monthly_due_dates(origination, as_of)
    installment = (exposure / max(len(due_dates), 1)).quantize(Decimal("0.01"))

    if profile == DelinquencyProfile.CLEAN:
        return [PaymentRecord(d, d, installment, installment) for d in due_dates]

    if profile == DelinquencyProfile.CHARGED_OFF:
        # Pays on time until `trouble_due`, then never pays again — past the 120-day
        # charge-off threshold by AS_OF_DATE (see _TROUBLE_AGE_BAND).
        assert trouble_due is not None
        delinquency_start = trouble_due
        return [
            PaymentRecord(d, d, installment, installment)
            if d < delinquency_start
            else PaymentRecord(d, None, installment, Decimal("0"))
            for d in due_dates
        ]

    # DELINQUENT_30/60/90/CURED: one installment goes unpaid at the point that lands
    # as_of at the target DPD; CURED additionally pays that installment late, just before
    # as_of, so the state machine has cured by the time the snapshot is taken.
    assert trouble_due is not None

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
            exposure = Decimal(str(round(rng.lognormvariate(8.5, 0.6), 2)))
            profile = rng.choices(profiles, weights=weights)[0]
            trouble_due: date | None = None
            if profile == DelinquencyProfile.CLEAN:
                origination = ORIGINATION_WINDOW_START + timedelta(days=rng.randint(0, span_days))
            else:
                # Anchor origination on the trouble installment so its age at AS_OF_DATE
                # lands in the profile's band, instead of picking origination first and
                # snapping to whichever due date happens to be nearby.
                trouble_due = _trouble_due_date(profile, AS_OF_DATE, rng)
                origination = _shift_months_back(
                    trouble_due,
                    rng.randint(_MIN_CLEAN_MONTHS_BEFORE_TROUBLE, _MAX_CLEAN_MONTHS_BEFORE_TROUBLE),
                )
            payments = _generate_payments(
                origination, AS_OF_DATE, profile, exposure, rng, trouble_due
            )
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
