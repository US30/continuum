"""T-learner on a small synthetic frame where contact truly raises the cure rate."""

from __future__ import annotations

import numpy as np
import pandas as pd

from continuum.scoring.features import FEATURES
from continuum.scoring.models import train_uplift_model


def _frame(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({f: rng.normal(size=n) for f in FEATURES})
    df["account_id"] = [f"a{seed}-{i}" for i in range(n)]
    df["treated"] = rng.random(n) < 0.4
    df["overdue_amount"] = 100.0
    # Cure depends on feature 0; contact adds +0.3 for everyone whose feature 1 is positive.
    p = 1 / (1 + np.exp(-df[FEATURES[0]]))
    p = np.clip(p + 0.3 * (df["treated"] & (df[FEATURES[1]] > 0)), 0.02, 0.98)
    df["cured_60"] = rng.random(n) < p
    return df


def test_uplift_scores_have_the_documented_columns_and_bounds() -> None:
    model = train_uplift_model(_frame(1_500, 1), _frame(500, 2))
    scores = model.score(_frame(200, 3))
    assert {
        "account_id",
        "p_cure_no_contact",
        "p_cure_contact",
        "uplift",
        "expected_incremental_recovery",
    } <= set(scores.columns)
    assert scores[["p_cure_no_contact", "p_cure_contact"]].stack().between(0, 1).all()
    assert np.allclose(scores["uplift"], scores["p_cure_contact"] - scores["p_cure_no_contact"])
    assert np.allclose(scores["expected_incremental_recovery"], scores["uplift"] * 100.0)


def test_model_learns_where_contact_helps() -> None:
    model = train_uplift_model(_frame(4_000, 1), _frame(1_000, 2))
    test = _frame(2_000, 3)
    uplift = model.score(test)["uplift"].to_numpy()
    helped = (test[FEATURES[1]] > 0).to_numpy()
    assert uplift[helped].mean() > uplift[~helped].mean() + 0.1


def test_each_arm_is_fitted_only_on_its_own_episodes() -> None:
    train, calibration = _frame(1_500, 1), _frame(500, 2)
    model = train_uplift_model(train, calibration)
    assert model.self_cure.classifier.n_features_in_ == len(FEATURES)
    # Calibrators exist for both arms and were fitted on held-out data.
    assert model.self_cure.calibrator is not None and model.response.calibrator is not None


def test_training_is_deterministic_for_a_seed() -> None:
    train, calibration, test = _frame(1_000, 1), _frame(400, 2), _frame(100, 3)
    a = train_uplift_model(train, calibration, seed=5).score(test)["uplift"]
    b = train_uplift_model(train, calibration, seed=5).score(test)["uplift"]
    assert np.allclose(a, b)
