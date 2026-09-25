"""Tests for the historical contact-policy simulation.

CLAUDE.md §8 calls this module "the highest-risk item in the whole build... scheduled
first deliberately" — hence the heavier coverage here than other Phase 1 modules: quiet-hour
adherence, determinism, and exposure-weighted probability. Whether the policy's contact
actually *changes the outcome* is tested in test_generate.py.
"""

from __future__ import annotations

import random
import uuid
from datetime import date, datetime

from continuum.config.settings import Settings
from continuum.foundation.contact_policy import (
    contact_probability,
    is_quiet_hours,
    simulate_decision_contact,
)

SETTINGS = Settings()


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


class TestSimulateDecisionContact:
    def _contact(self, seed: int = 1, cured: bool = False):
        return simulate_decision_contact(
            uuid.uuid4(), uuid.uuid4(), date(2025, 6, 10), cured, SETTINGS, random.Random(seed)
        )

    def test_sent_on_the_decision_date(self) -> None:
        assert self._contact().sent_at.date() == date(2025, 6, 10)

    def test_respects_quiet_hours_across_many_draws(self) -> None:
        for seed in range(200):
            assert not is_quiet_hours(self._contact(seed).sent_at, SETTINGS)

    def test_deterministic_given_same_seed(self) -> None:
        entity_id, account_id = uuid.uuid4(), uuid.uuid4()

        def make() -> object:
            return simulate_decision_contact(
                entity_id, account_id, date(2025, 6, 10), True, SETTINGS, random.Random(42)
            )

        assert make() == make()

    def test_cured_accounts_respond_more_often(self) -> None:
        cured = sum(self._contact(s, True).responded for s in range(400))
        not_cured = sum(self._contact(s, False).responded for s in range(400))
        assert cured > not_cured

    def test_response_follows_the_contact(self) -> None:
        for seed in range(100):
            contact = self._contact(seed, True)
            if contact.responded:
                assert contact.responded_at is not None
                assert contact.responded_at > contact.sent_at
            else:
                assert contact.responded_at is None
