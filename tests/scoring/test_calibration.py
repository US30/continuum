from __future__ import annotations

import numpy as np
import pytest

from continuum.scoring import calibration


def test_brier_of_perfect_and_worst_predictions() -> None:
    y = np.array([1, 0, 1, 0])
    assert calibration.brier(y, np.array([1.0, 0.0, 1.0, 0.0])) == 0.0
    assert calibration.brier(y, np.array([0.0, 1.0, 0.0, 1.0])) == 1.0


def test_reliability_bins_are_equal_count_and_cover_everything() -> None:
    p = np.linspace(0.01, 0.99, 100)
    y = (p > 0.5).astype(int)
    bins = calibration.reliability_curve(y, p, n_bins=10)
    assert len(bins) == 10 and all(b.count == 10 for b in bins)
    assert sum(b.count for b in bins) == 100
    assert [b.mean_predicted for b in bins] == sorted(b.mean_predicted for b in bins)


def test_reliability_of_empty_input_is_empty() -> None:
    assert calibration.reliability_curve(np.array([], dtype=int), np.array([])) == []


def test_ece_is_zero_when_predictions_match_observed_rates() -> None:
    bins = [calibration.ReliabilityBin(0.2, 0.2, 50), calibration.ReliabilityBin(0.8, 0.8, 50)]
    assert calibration.expected_calibration_error(bins) == 0.0


def test_platt_scaling_fixes_an_overconfident_model() -> None:
    rng = np.random.default_rng(0)
    true_p = rng.uniform(0.2, 0.8, 4_000)
    y = (rng.random(4_000) < true_p).astype(int)
    # An overconfident model: pushes probabilities toward 0 and 1.
    logit = np.log(true_p / (1 - true_p))
    overconfident = 1 / (1 + np.exp(-3 * logit))

    calibrator = calibration.PlattCalibrator().fit(overconfident[:2_000], y[:2_000])
    fixed = calibrator.transform(overconfident[2_000:])

    before = calibration.brier(y[2_000:], overconfident[2_000:])
    after = calibration.brier(y[2_000:], fixed)
    assert after < before
    ece_after = calibration.expected_calibration_error(
        calibration.reliability_curve(y[2_000:], fixed)
    )
    assert ece_after == pytest.approx(0.0, abs=0.04)


def test_platt_output_is_a_probability() -> None:
    calibrator = calibration.PlattCalibrator().fit(
        np.array([0.1, 0.4, 0.6, 0.9]), np.array([0, 0, 1, 1])
    )
    out = calibrator.transform(np.array([0.0, 0.5, 1.0]))
    assert ((out >= 0) & (out <= 1)).all()
