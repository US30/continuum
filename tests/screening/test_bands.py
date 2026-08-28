"""Pure tests for deterministic screening band logic. No DB, no LLM."""

from __future__ import annotations

from continuum.config.settings import Settings
from continuum.screening.bands import ScreeningBand, band_for_score

SETTINGS = Settings()  # floor=0.15, ceiling=0.92 by default


class TestBandForScore:
    def test_below_floor_is_auto_reject(self) -> None:
        assert band_for_score(0.05, SETTINGS) == ScreeningBand.AUTO_REJECT

    def test_at_floor_is_auto_reject(self) -> None:
        assert band_for_score(SETTINGS.screening_auto_reject_floor, SETTINGS) == ScreeningBand.AUTO_REJECT

    def test_just_above_floor_is_adjudicate(self) -> None:
        score = SETTINGS.screening_auto_reject_floor + 0.001
        assert band_for_score(score, SETTINGS) == ScreeningBand.ADJUDICATE

    def test_middle_is_adjudicate(self) -> None:
        assert band_for_score(0.5, SETTINGS) == ScreeningBand.ADJUDICATE

    def test_just_below_ceiling_is_adjudicate(self) -> None:
        score = SETTINGS.screening_auto_confirm_ceiling - 0.001
        assert band_for_score(score, SETTINGS) == ScreeningBand.ADJUDICATE

    def test_at_ceiling_is_auto_confirm(self) -> None:
        assert band_for_score(SETTINGS.screening_auto_confirm_ceiling, SETTINGS) == ScreeningBand.AUTO_CONFIRM

    def test_above_ceiling_is_auto_confirm(self) -> None:
        assert band_for_score(1.0, SETTINGS) == ScreeningBand.AUTO_CONFIRM

    def test_misconfigured_thresholds_prefer_auto_confirm_over_auto_reject(self) -> None:
        # If ceiling <= floor were ever misconfigured, a candidate satisfying both
        # conditions must resolve to AUTO_CONFIRM (checked first), never fall through to
        # AUTO_REJECT — silently rejecting a high scorer would be an I5 violation.
        misconfigured = Settings(
            screening_auto_reject_floor=0.9, screening_auto_confirm_ceiling=0.2
        )
        assert band_for_score(0.95, misconfigured) == ScreeningBand.AUTO_CONFIRM
