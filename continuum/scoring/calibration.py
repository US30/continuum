"""Probability calibration and reliability diagnostics.

CLAUDE.md §8 Phase 4: "Calibration with reliability curves and Brier score." Uplift is a
*difference* of two predicted probabilities, so a systematically over- or under-confident arm
turns directly into a biased uplift; each arm is calibrated separately.

Method: Platt scaling (logistic regression on the logit of the raw probability) fitted on a
held-out calibration slice. Chosen over isotonic regression because the calibration slice is
small per arm (a few hundred episodes) and isotonic overfits step functions at that size.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss

FloatArray = NDArray[np.float64]

_EPS = 1e-6


def _logit(p: FloatArray) -> FloatArray:
    clipped = np.clip(p, _EPS, 1 - _EPS)
    return np.log(clipped / (1 - clipped))


class PlattCalibrator:
    def __init__(self) -> None:
        self._model = LogisticRegression(C=1e6, solver="lbfgs")

    def fit(self, raw: FloatArray, y: NDArray[np.int_]) -> PlattCalibrator:
        self._model.fit(_logit(raw).reshape(-1, 1), y)
        return self

    def transform(self, raw: FloatArray) -> FloatArray:
        out: FloatArray = self._model.predict_proba(_logit(raw).reshape(-1, 1))[:, 1]
        return out


@dataclass(frozen=True)
class ReliabilityBin:
    mean_predicted: float
    observed_rate: float
    count: int


def brier(y: NDArray[np.int_], p: FloatArray) -> float:
    return float(brier_score_loss(y, p))


def reliability_curve(y: NDArray[np.int_], p: FloatArray, n_bins: int = 10) -> list[ReliabilityBin]:
    """Equal-count bins by predicted probability; empty bins are dropped."""
    if len(p) == 0:
        return []
    order = np.argsort(p)
    bins: list[ReliabilityBin] = []
    for chunk in np.array_split(order, min(n_bins, len(p))):
        if len(chunk) == 0:
            continue
        bins.append(
            ReliabilityBin(
                mean_predicted=float(p[chunk].mean()),
                observed_rate=float(y[chunk].mean()),
                count=len(chunk),
            )
        )
    return bins


def expected_calibration_error(bins: list[ReliabilityBin]) -> float:
    total = sum(b.count for b in bins)
    if total == 0:
        return 0.0
    return sum(b.count * abs(b.mean_predicted - b.observed_rate) for b in bins) / total
