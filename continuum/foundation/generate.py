"""Seeded synthetic loan-and-payment generator with causal delinquency episodes.

CLAUDE.md decision (see plan): the loan dataset is fully synthetic, generated in-process
from Settings.data_seed rather than downloaded, so the whole build is reproducible with no
external dependency (CLAUDE.md I6, I8). Emits installment-level payment history — that is
what makes the SCD2 delinquency machine (continuum.foundation.delinquency) derivable
rather than invented directly.

Delinquency is generated as an *episode* per account: one installment goes unpaid, the
account reaches 30 DPD on the decision date, the historical rules policy contacts it (or
not), and whether it cures within 60 days is drawn from continuum.foundation.causal — so the
contact genuinely changes the outcome. The delinquency_state rows are still always derived
from the payments by continuum.foundation.delinquency, never assigned.
"""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from continuum.config.settings import get_settings
from continuum.foundation import causal
from continuum.foundation.contact_policy import contact_probability
from continuum.foundation.delinquency import PaymentRecord

# The synthetic dataset's fixed "as of" date. Every derived delinquency state and every
# simulated contact is computed relative to this date, never the wall clock — required for
# CLAUDE.md I6 (idempotent, re-runnable on the same inputs with identical output).
AS_OF_DATE = get_settings().data_as_of_date

ORIGINATION_WINDOW_START = date(2024, 1, 1)
ORIGINATION_WINDOW_END = date(2025, 10, 1)

PRODUCTS = ("personal_loan", "auto_loan", "credit_card")

# --- Shape of the synthetic dataset (not runtime business thresholds, CLAUDE.md §10) ---
# Exposure ~ lognormal(mu, sigma); z_exposure standardises against these.
EXPOSURE_LOG_MU = 8.5
EXPOSURE_LOG_SIGMA = 0.6

# P(account has a delinquency episode) = sigmoid(intercept + slope * discipline): less
# disciplined borrowers default more often (~55% overall).
ENTER_EPISODE_INTERCEPT = 0.3
ENTER_EPISODE_DISCIPLINE = -0.7

# Age, in days before AS_OF_DATE, of the first unpaid installment. Lower bound keeps the
# decision date (due + 30) on or before AS_OF_DATE; episodes younger than 90 days have a
# still-open 60-day window at AS_OF_DATE (censored for modelling).
TROUBLE_AGE_MIN_DAYS = 32
TROUBLE_AGE_MAX_DAYS = 330

# On-time installments before the episode.
MIN_MONTHS_BEFORE_TROUBLE = 1
MAX_MONTHS_BEFORE_TROUBLE = 10
MONTHS_ON_BOOK_MEAN = (MIN_MONTHS_BEFORE_TROUBLE + MAX_MONTHS_BEFORE_TROUBLE) / 2
MONTHS_ON_BOOK_SD = 2.9  # sd of a discrete uniform on 1..10

# Observable proxy for the latent discipline: how often, and by how many days, earlier
# installments were paid late. P(late) = sigmoid(intercept + slope * discipline).
PRIOR_LATE_INTERCEPT = -1.2
PRIOR_LATE_DISCIPLINE = -1.0
PRIOR_LATE_MAX_DAYS = 12  # stays inside the 30-DPD grace, so it never becomes delinquency

# --- Shared-contact structures (Phase 7: what an account-centric view cannot see) ---
# Households: 2-4 people sharing an address (benign; some also share a landline). Fraud rings:
# 4-6 distinct people sharing a phone AND an address, all going delinquent within a short
# window and almost never curing (a synthetic-identity / bust-out pattern). Fractions are of
# persons; consecutive persons form a group, so grouping is deterministic given the seed.
HOUSEHOLD_START_PROBABILITY = 0.022  # ~6-7% of persons end up in a household
HOUSEHOLD_SIZES = (2, 3, 4)
RING_START_PROBABILITY = 0.006  # ~3% of persons (~20 rings per 5,000) end up in a ring
RING_SIZES = (4, 5, 6)
RING_ACCOUNTS_PER_PERSON_WEIGHTS = (0.6, 0.4)  # 1 or 2 accounts
RING_ENTER_EPISODE_PROBABILITY = 0.95
RING_CURE_PROBABILITY = 0.05  # regardless of contact: fraud does not respond to outreach
RING_TROUBLE_AGE_SPREAD_DAYS = 10  # members go delinquent within ~3 weeks of each other

DECISION_LAG_DAYS = 30  # first reaches 30 DPD this many days after the unpaid due date
CURE_MIN_DAYS = 3
LATE_CURE_MIN_DAYS = causal.CURE_WINDOW_DAYS + 1
LATE_CURE_MAX_DAYS = 89  # < 120 - 30: pays before charge-off


@dataclass(frozen=True)
class Episode:
    """One delinquency episode and its (oracle) causal draw."""

    decision_date: date  # first day at 30 DPD; treatment happens on this day
    treated: bool  # contacted by the historical rules policy
    p_cure_control: float  # oracle: P(cure within 60d | no contact)
    p_cure_treated: float  # oracle: P(cure within 60d | contact)
    cured_60: bool  # latent realised outcome (may lie beyond AS_OF_DATE for late episodes)
    cure_date: date | None
    overdue_amount: Decimal  # installments overdue on decision_date: the value of a cure


@dataclass(frozen=True)
class SyntheticAccount:
    account_id: uuid.UUID
    product: str
    exposure: Decimal
    origination_date: date
    episode: Episode | None
    payments: list[PaymentRecord]


@dataclass(frozen=True)
class SyntheticPerson:
    person_id: uuid.UUID
    accounts: list[SyntheticAccount]
    #: "household" | "ring" | None. Eval-only ground truth; see continuum.foundation.eval_truth.
    group_type: str | None = None
    group_id: str | None = None


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
    """`d` minus `months` calendar months. `d.day` must be <= 28."""
    total = d.year * 12 + (d.month - 1) - months
    return date(total // 12, total % 12 + 1, d.day)


def _prior_paid_date(due: date, discipline: float, as_of: date, rng: random.Random) -> date:
    """Paid on time or (less-disciplined borrowers more often) a few days late."""
    p_late = causal.sigmoid(PRIOR_LATE_INTERCEPT + PRIOR_LATE_DISCIPLINE * discipline)
    if rng.random() < p_late:
        return min(due + timedelta(days=rng.randint(1, PRIOR_LATE_MAX_DAYS)), as_of)
    return due


def _generate_account(
    discipline: float, rng: random.Random, ring_anchor_age: int | None = None
) -> SyntheticAccount:
    settings = get_settings()
    exposure = Decimal(str(round(rng.lognormvariate(EXPOSURE_LOG_MU, EXPOSURE_LOG_SIGMA), 2)))
    z_exposure = (math.log(float(exposure)) - EXPOSURE_LOG_MU) / EXPOSURE_LOG_SIGMA
    if ring_anchor_age is not None:
        has_episode = rng.random() < RING_ENTER_EPISODE_PROBABILITY
    else:
        has_episode = rng.random() < causal.sigmoid(
            ENTER_EPISODE_INTERCEPT + ENTER_EPISODE_DISCIPLINE * discipline
        )

    episode_meta: tuple[date, int] | None = None
    if has_episode:
        if ring_anchor_age is None:
            age = rng.randint(TROUBLE_AGE_MIN_DAYS, TROUBLE_AGE_MAX_DAYS)
        else:
            age = ring_anchor_age + rng.randint(
                -RING_TROUBLE_AGE_SPREAD_DAYS, RING_TROUBLE_AGE_SPREAD_DAYS
            )
            age = min(max(age, TROUBLE_AGE_MIN_DAYS), TROUBLE_AGE_MAX_DAYS)
        due = AS_OF_DATE - timedelta(days=age)
        trouble_due = due.replace(day=min(due.day, 28))
        months_before = rng.randint(MIN_MONTHS_BEFORE_TROUBLE, MAX_MONTHS_BEFORE_TROUBLE)
        origination = _shift_months_back(trouble_due, months_before)
        episode_meta = (trouble_due, months_before)
    else:
        span_days = (ORIGINATION_WINDOW_END - ORIGINATION_WINDOW_START).days
        origination = ORIGINATION_WINDOW_START + timedelta(days=rng.randint(0, span_days))

    due_dates = _monthly_due_dates(origination, AS_OF_DATE)
    installment = (exposure / max(len(due_dates), 1)).quantize(Decimal("0.01"))
    unpaid = Decimal("0")

    if episode_meta is None:
        clean_payments = [
            PaymentRecord(
                d, _prior_paid_date(d, discipline, AS_OF_DATE, rng), installment, installment
            )
            for d in due_dates
        ]
        return SyntheticAccount(
            account_id=uuid.UUID(int=rng.getrandbits(128)),
            product=rng.choice(PRODUCTS),
            exposure=exposure,
            origination_date=origination,
            episode=None,
            payments=clean_payments,
        )

    trouble_due, months_before = episode_meta
    decision_date = trouble_due + timedelta(days=DECISION_LAG_DAYS)
    z_months = (months_before - MONTHS_ON_BOOK_MEAN) / MONTHS_ON_BOOK_SD

    if ring_anchor_age is None:
        p_control, p_treated = causal.cure_probabilities(discipline, z_exposure, z_months)
    else:
        p_control = p_treated = RING_CURE_PROBABILITY
    treated = rng.random() < contact_probability(float(exposure), settings)
    cured_60 = rng.random() < (p_treated if treated else p_control)

    cure_date: date | None = None
    if cured_60:
        cure_date = decision_date + timedelta(
            days=rng.randint(CURE_MIN_DAYS, causal.CURE_WINDOW_DAYS)
        )
    elif rng.random() < causal.LATE_CURE_PROBABILITY:
        cure_date = decision_date + timedelta(
            days=rng.randint(LATE_CURE_MIN_DAYS, LATE_CURE_MAX_DAYS)
        )
    # A cure that lands after AS_OF_DATE has not happened yet in the data.
    observed_cure = cure_date if cure_date is not None and cure_date <= AS_OF_DATE else None

    payments: list[PaymentRecord] = []
    overdue_amount = Decimal("0")
    for d in due_dates:
        if d < trouble_due:
            payments.append(
                PaymentRecord(
                    d, _prior_paid_date(d, discipline, AS_OF_DATE, rng), installment, installment
                )
            )
            continue
        if d <= decision_date:
            overdue_amount += installment  # unpaid on the decision date: what a cure recovers
        if observed_cure is None:
            payments.append(PaymentRecord(d, None, installment, unpaid))
        elif d <= observed_cure:
            # A cure catches the account up: every installment already due is paid that day.
            payments.append(PaymentRecord(d, observed_cure, installment, installment))
        else:
            payments.append(PaymentRecord(d, d, installment, installment))  # on time after

    return SyntheticAccount(
        account_id=uuid.UUID(int=rng.getrandbits(128)),
        product=rng.choice(PRODUCTS),
        exposure=exposure,
        origination_date=origination,
        episode=Episode(
            decision_date=decision_date,
            treated=treated,
            p_cure_control=p_control,
            p_cure_treated=p_treated,
            cured_60=cured_60,
            cure_date=cure_date,
            overdue_amount=overdue_amount,
        ),
        payments=payments,
    )


def _assign_groups(n_persons: int, rng: random.Random) -> list[tuple[str | None, str | None]]:
    """(group_type, group_id) per person, consecutive persons forming each group."""
    groups: list[tuple[str | None, str | None]] = []
    counter = 0
    while len(groups) < n_persons:
        roll = rng.random()
        if roll < RING_START_PROBABILITY:
            kind, size = "ring", rng.choice(RING_SIZES)
        elif roll < RING_START_PROBABILITY + HOUSEHOLD_START_PROBABILITY:
            kind, size = "household", rng.choice(HOUSEHOLD_SIZES)
        else:
            groups.append((None, None))
            continue
        counter += 1
        groups.extend([(kind, f"{kind}-{counter:05d}")] * size)
    return groups[:n_persons]


def generate_population(n_persons: int, seed: int) -> list[SyntheticPerson]:
    """Deterministic given (n_persons, seed) — required for CLAUDE.md I6 idempotency."""
    rng = random.Random(seed)
    group_of = _assign_groups(n_persons, random.Random(seed + 7))
    ring_anchor: dict[str, int] = {}

    persons: list[SyntheticPerson] = []
    for group_type, group_id in group_of:
        person_id = uuid.UUID(int=rng.getrandbits(128))
        discipline = rng.gauss(0.0, 1.0)  # latent, never persisted
        anchor: int | None = None
        if group_type == "ring" and group_id is not None:
            if group_id not in ring_anchor:
                ring_anchor[group_id] = rng.randint(70, 250)
            anchor = ring_anchor[group_id]
            n_accounts = rng.choices([1, 2], weights=RING_ACCOUNTS_PER_PERSON_WEIGHTS)[0]
        else:
            n_accounts = rng.choices([1, 2, 3], weights=[0.7, 0.25, 0.05])[0]
        accounts = [_generate_account(discipline, rng, anchor) for _ in range(n_accounts)]
        persons.append(
            SyntheticPerson(
                person_id=person_id, accounts=accounts, group_type=group_type, group_id=group_id
            )
        )
    return persons
