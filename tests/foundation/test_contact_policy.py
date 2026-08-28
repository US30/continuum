"""Tests for the historical contact-policy simulation.

CLAUDE.md §8 calls this module "the highest-risk item in the whole build... scheduled
first deliberately" — hence the heavier coverage here than other Phase 1 modules: cap
adherence, quiet-hour adherence, determinism, and a non-degenerate treatment rate.
"""

from __future__ import annotations

import random
import uuid
from datetime import date, datetime

from continuum.config.settings import Settings
from continuum.db.models.account import DelinquencyBucket
from continuum.foundation.contact_policy import (
    contact_probability,
    is_quiet_hours,
    simulate_contacts,
)
from continuum.foundation.delinquency import StateInterval

SETTINGS = Settings()


def _interval(state: DelinquencyBucket, day: int) -> StateInterval:
    return StateInterval(
        effective_from=date(2025, 1, day),
        effective_to=None,
        state=state,
        days_past_due=30,
    )


class TestContactProbability:
    def test_monotonic_in_exposure(self) -> None:
        low = contact_probability(1_000, SETTINGS)
        high = contact_probability(15_000, SETTINGS)
        assert high > low

    def test_capped_at_point_nine(self) -> None:
        assert contact_probability(10_000_000, SETTINGS) == 0.9

    def test_never_negative_or_over_one(self) -> None:
        for exposure in (0, 1, 500, 50_000, 5_000_000):
            p = contact_probability(exposure, SETTINGS)
            assert 0.0 <= p <= 0.9


class TestQuietHours:
    def test_inside_quiet_window_wrapping_midnight(self) -> None:
        # Settings default: quiet 21:00-08:00.
        assert is_quiet_hours(datetime(2025, 1, 1, 22, 0), SETTINGS) is True
        assert is_quiet_hours(datetime(2025, 1, 1, 3, 0), SETTINGS) is True

    def test_outside_quiet_window(self) -> None:
        assert is_quiet_hours(datetime(2025, 1, 1, 14, 0), SETTINGS) is False

    def test_boundary_hours(self) -> None:
        assert is_quiet_hours(datetime(2025, 1, 1, 21, 0), SETTINGS) is True
        assert is_quiet_hours(datetime(2025, 1, 1, 8, 0), SETTINGS) is False


class TestSimulateContacts:
    def test_no_contact_outside_trigger_buckets(self) -> None:
        states = [_interval(DelinquencyBucket.CURRENT, 1), _interval(DelinquencyBucket.CURED, 40)]
        contacts = simulate_contacts(
            uuid.uuid4(), uuid.uuid4(), 10_000, states, SETTINGS, random.Random(1)
        )
        assert contacts == []

    def test_sent_contacts_respect_quiet_hours(self) -> None:
        states = [_interval(DelinquencyBucket.DPD_30, d) for d in range(1, 29, 3)]
        contacts = simulate_contacts(
            uuid.uuid4(), uuid.uuid4(), 15_000, states, SETTINGS, random.Random(7)
        )
        for contact in contacts:
            assert not is_quiet_hours(contact.sent_at, SETTINGS)

    def test_frequency_cap_enforced(self) -> None:
        # Every day in a trigger bucket, high exposure so probability is near-certain.
        states = [_interval(DelinquencyBucket.DPD_30, d) for d in range(1, 29)]
        contacts = simulate_contacts(
            uuid.uuid4(), uuid.uuid4(), 1_000_000, states, SETTINGS, random.Random(3)
        )
        min_gap = max(1, 7 // max(SETTINGS.contact_policy_max_contacts_per_week, 1))
        for earlier, later in zip(contacts, contacts[1:], strict=False):
            assert (later.sent_at.date() - earlier.sent_at.date()).days >= min_gap

    def test_deterministic_given_same_seed(self) -> None:
        states = [_interval(DelinquencyBucket.DPD_30, d) for d in range(1, 20, 4)]
        entity_id, account_id = uuid.uuid4(), uuid.uuid4()
        first = simulate_contacts(
            entity_id, account_id, 12_000, states, SETTINGS, random.Random(42)
        )
        second = simulate_contacts(
            entity_id, account_id, 12_000, states, SETTINGS, random.Random(42)
        )
        assert first == second

    def test_treatment_rate_is_non_degenerate_across_exposure_bands(self) -> None:
        # Across many independent accounts spanning low to high exposure, some fraction
        # must be contacted and some must not — neither always nor never (a degenerate
        # policy would make the Phase 4 uplift model untrainable).
        rng = random.Random(99)
        contacted = 0
        total = 200
        for i in range(total):
            exposure = 500 + i * 500
            states = [_interval(DelinquencyBucket.DPD_30, 15)]
            contacts = simulate_contacts(
                uuid.uuid4(), uuid.uuid4(), exposure, states, SETTINGS, rng
            )
            if contacts:
                contacted += 1
        rate = contacted / total
        assert 0.05 < rate < 0.95
