"""Pure tests for the golden-set loader and the metric arithmetic. The part that needs a
live Postgres (pg_trgm's similarity()) is in compute_screening_metrics, exercised only via
`make screen-metrics` — not unit tested here, consistent with how eval.resolution_metrics'
arithmetic is unit tested separately from its DB-touching query.
"""

from __future__ import annotations

import pytest

from eval.screening_metrics import ScreeningMetrics, load_golden_set


class TestLoadGoldenSet:
    def test_loads_all_rows(self) -> None:
        pairs = load_golden_set()
        assert len(pairs) == 18

    def test_case_types_cover_the_required_categories(self) -> None:
        # CLAUDE.md §8: "transliterations, common names, DOB near-misses."
        pairs = load_golden_set()
        case_types = {p.case_type for p in pairs}
        assert {"transliteration", "common_name", "dob_near_miss"} <= case_types

    def test_includes_both_true_and_false_matches(self) -> None:
        pairs = load_golden_set()
        assert any(p.is_true_match for p in pairs)
        assert any(not p.is_true_match for p in pairs)

    def test_case_ids_are_unique(self) -> None:
        pairs = load_golden_set()
        ids = [p.case_id for p in pairs]
        assert len(ids) == len(set(ids))


class TestGoldenSetDobs:
    def test_dobs_are_parsed(self) -> None:
        pair = next(p for p in load_golden_set() if p.case_id == "hp-011")
        assert pair.query_dob is not None and pair.listing_dob is not None


class TestScreeningMetricsArithmetic:
    def test_proportion_reaching_model_counts_only_adjudicated(self) -> None:
        m = ScreeningMetrics(
            true_positive=3, false_negative=0, flagged=6, total=12, auto_confirmed=2, adjudicated=4
        )
        assert m.proportion_reaching_model == pytest.approx(4 / 12)

    def test_final_stage_none_until_adjudicator_run(self) -> None:
        m = ScreeningMetrics(true_positive=1, false_negative=0, flagged=1, total=1)
        assert m.final_recall is None and m.final_precision is None

    def test_final_precision_and_recall(self) -> None:
        m = ScreeningMetrics(
            true_positive=4,
            false_negative=0,
            flagged=6,
            total=8,
            final_true_positive=3,
            final_false_negative=1,
            final_positive=4,
        )
        assert m.final_recall == 0.75
        assert m.final_precision == 0.75

    def test_perfect_recall_and_precision(self) -> None:
        metrics = ScreeningMetrics(true_positive=5, false_negative=0, flagged=5, total=10)
        assert metrics.recall == 1.0
        assert metrics.precision == 1.0

    def test_missed_true_match_lowers_recall(self) -> None:
        metrics = ScreeningMetrics(true_positive=4, false_negative=1, flagged=4, total=10)
        assert metrics.recall == 0.8

    def test_flagged_negatives_lower_precision_not_recall(self) -> None:
        metrics = ScreeningMetrics(true_positive=5, false_negative=0, flagged=8, total=10)
        assert metrics.recall == 1.0
        assert metrics.precision == pytest.approx(5 / 8)

    def test_no_true_matches_returns_none_recall(self) -> None:
        metrics = ScreeningMetrics(true_positive=0, false_negative=0, flagged=0, total=0)
        assert metrics.recall is None
        assert metrics.precision is None
