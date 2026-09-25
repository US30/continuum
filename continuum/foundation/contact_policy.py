"""Historical contact-policy simulation — the treatment variable for Phase 4 uplift models.

CLAUDE.md §8: "Simulate a historical contact policy (rules-based: contact at 30 DPD
weighted by exposure) so the uplift models have a treatment variable." CLAUDE.md also
calls this "the highest-risk item in the whole build... scheduled first deliberately.
Do not defer it."

The policy is exactly that sentence: on the day an account first reaches 30 DPD (its
decision date), contact it with a probability that rises with exposure. Whether a contact
happens is decided in continuum.foundation.generate (it must be drawn *before* the outcome,
which depends on it — see continuum.foundation.causal); this module owns the policy
function, the quiet-hours rule, and the contact record's timestamp/channel/response.

Quiet hours reuse the exact named settings the Phase 6 guard will enforce at runtime
(Settings.contact_policy_*), so history and live policy never silently diverge. The weekly
frequency cap is a guard concern (one contact per episode here, so it cannot bind).
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from continuum.config.settings import Settings
from continuum.db.models.collections import ContactChannel

# Probability that a contacted account responds, by outcome. Response is a downstream
# engagement signal for the (optional) response model; it is *not* the treatment effect,
# which lives on cure (continuum.foundation.causal).
RESPONSE_RATE_IF_CURED = 0.7
RESPONSE_RATE_IF_NOT_CURED = 0.2
RESPONSE_MAX_DAYS = 5


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


def simulate_decision_contact(
    entity_id: uuid.UUID,
    account_id: uuid.UUID,
    decision_date: date,
    cured_60: bool,
    settings: Settings,
    rng: random.Random,
) -> SimulatedContact:
    """The one contact the rules policy sends for an episode, on its decision date, outside
    quiet hours.
    """
    sent_at = datetime.combine(decision_date, time(hour=rng.randint(9, 20)))
    if is_quiet_hours(sent_at, settings):
        safe_hour = (settings.contact_policy_quiet_hours_end + 1) % 24
        sent_at = sent_at.replace(hour=safe_hour)

    responded = rng.random() < (RESPONSE_RATE_IF_CURED if cured_60 else RESPONSE_RATE_IF_NOT_CURED)
    responded_at = (
        sent_at + timedelta(days=rng.randint(1, RESPONSE_MAX_DAYS)) if responded else None
    )
    return SimulatedContact(
        entity_id=entity_id,
        account_id=account_id,
        channel=rng.choice([ContactChannel.SMS, ContactChannel.EMAIL]),
        sent_at=sent_at,
        responded=responded,
        responded_at=responded_at,
    )
