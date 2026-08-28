"""Historical contact-policy simulation — the treatment variable for Phase 4 uplift models.

CLAUDE.md §8: "Simulate a historical contact policy (rules-based: contact at 30 DPD
weighted by exposure) so the uplift models have a treatment variable." CLAUDE.md also
calls this "the highest-risk item in the whole build... scheduled first deliberately.
Do not defer it." — hence the heavier test coverage in
tests/foundation/test_contact_policy.py relative to the other Phase 1 modules.

The rules simulated here (trigger DPD, frequency cap, quiet hours) intentionally reuse the
exact named settings the Phase 6 guard will enforce at runtime (Settings.contact_policy_*),
so the historical treatment population and the live policy never silently diverge.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from continuum.config.settings import Settings
from continuum.db.models.account import DelinquencyBucket
from continuum.db.models.collections import ContactChannel
from continuum.foundation.delinquency import StateInterval

TRIGGER_BUCKETS = frozenset(
    {DelinquencyBucket.DPD_30, DelinquencyBucket.DPD_60, DelinquencyBucket.DPD_90}
)


@dataclass(frozen=True)
class SimulatedContact:
    entity_id: uuid.UUID
    account_id: uuid.UUID
    channel: ContactChannel
    sent_at: datetime
    responded: bool
    responded_at: datetime | None


def contact_probability(exposure: float, settings: Settings) -> float:
    """Monotonically increasing in exposure, capped at 0.9. This *is* the rules-based
    baseline (CLAUDE.md §8) — it only needs to be monotonic and bounded, not tuned; Phase 4
    backtests uplift-ranking recovery against exactly this baseline, so its shape must stay
    simple and legible rather than optimised.
    """
    return min(0.9, 0.1 + float(exposure) / 20_000)


def is_quiet_hours(moment: datetime, settings: Settings) -> bool:
    hour = moment.hour
    start = settings.contact_policy_quiet_hours_start
    end = settings.contact_policy_quiet_hours_end
    if start > end:  # window wraps past midnight, e.g. 21:00-08:00
        return hour >= start or hour < end
    return start <= hour < end


def simulate_contacts(
    entity_id: uuid.UUID,
    account_id: uuid.UUID,
    exposure: float,
    states: list[StateInterval],
    settings: Settings,
    rng: random.Random,
) -> list[SimulatedContact]:
    """Contact the account whenever it is in a trigger bucket, subject to the weekly
    frequency cap, quiet hours, and an exposure-weighted probability of being contacted
    at all. Only fires on delinquency *intervals*, matching how the real nightly batch
    would only see a bucket once it has already transitioned into it.
    """
    min_gap_days = max(1, 7 // max(settings.contact_policy_max_contacts_per_week, 1))

    contacts: list[SimulatedContact] = []
    last_contact_date = None

    for interval in states:
        if interval.state not in TRIGGER_BUCKETS:
            continue
        if last_contact_date is not None:
            if (interval.effective_from - last_contact_date).days < min_gap_days:
                continue
        if rng.random() >= contact_probability(exposure, settings):
            continue

        sent_at = datetime.combine(interval.effective_from, time(hour=rng.randint(9, 20)))
        if is_quiet_hours(sent_at, settings):
            safe_hour = (settings.contact_policy_quiet_hours_end + 1) % 24
            sent_at = sent_at.replace(hour=safe_hour)

        # Simulated response rate: a coarse proxy standing in for the real response model
        # (Phase 4) — contacts made earlier in the delinquency lifecycle land a response
        # more often than ones made once the account is already deep in DPD_90.
        response_rate = 0.35 if interval.state == DelinquencyBucket.DPD_30 else 0.15
        responded = rng.random() < response_rate
        responded_at = sent_at + timedelta(days=rng.randint(1, 5)) if responded else None

        contacts.append(
            SimulatedContact(
                entity_id=entity_id,
                account_id=account_id,
                channel=rng.choice([ContactChannel.SMS, ContactChannel.EMAIL]),
                sent_at=sent_at,
                responded=responded,
                responded_at=responded_at,
            )
        )
        last_contact_date = interval.effective_from

    return contacts
