"""Self-cure, response and uplift models (a T-learner).

CLAUDE.md §8 Phase 4 names two models: self-cure (P of cure with no contact) and response
(P of a good outcome given contact), with uplift = difference. To keep that difference a
genuine treatment effect, both models predict the *same* event — cure within 60 days — one
fitted on the untreated episodes (self-cure), one on the contacted ones (response). This
deviates from CLAUDE.md's literal "response P" (an engagement flag, a different event) and
was agreed with the user before building.

    uplift(x)              = P(cure | contact, x) - P(cure | no contact, x)
    expected_incremental   = uplift(x) * overdue_amount        (currency)

I1: no LLM anywhere here. Hyperparameters are fixed, conservative values (shallow trees,
min leaf size, subsampling) rather than tuned — with ~1k rows per arm, tuning would mostly
fit the validation split; not tuned against anything (CLAUDE.md §10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from numpy.typing import NDArray

from continuum.scoring.calibration import PlattCalibrator
from continuum.scoring.features import FEATURES

MODEL_VERSION = "uplift_tlearner_v1"

LGBM_PARAMS: dict[str, Any] = {
    "n_estimators": 150,
    "learning_rate": 0.05,
    "num_leaves": 8,
    "min_child_samples": 40,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 5.0,
    "verbose": -1,
}


@dataclass
class ArmModel:
    """One arm's classifier plus its calibrator."""

    classifier: LGBMClassifier
    calibrator: PlattCalibrator | None = None

    def raw(self, frame: pd.DataFrame) -> NDArray[np.float64]:
        probabilities = np.asarray(self.classifier.predict_proba(frame[FEATURES]), dtype=np.float64)
        out: NDArray[np.float64] = probabilities[:, 1]
        return out

    def predict(self, frame: pd.DataFrame) -> NDArray[np.float64]:
        raw = self.raw(frame)
        return raw if self.calibrator is None else self.calibrator.transform(raw)


@dataclass
class UpliftModel:
    self_cure: ArmModel  # fitted on untreated episodes: P(cure | no contact)
    response: ArmModel  # fitted on treated episodes:   P(cure | contact)
    version: str = MODEL_VERSION
    features: list[str] = field(default_factory=lambda: list(FEATURES))

    def score(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Adds p_cure_no_contact, p_cure_contact, uplift and expected_incremental_recovery."""
        p0 = self.self_cure.predict(frame)
        p1 = self.response.predict(frame)
        out = frame[["account_id"]].copy()
        out["p_cure_no_contact"] = p0
        out["p_cure_contact"] = p1
        out["uplift"] = p1 - p0
        out["expected_incremental_recovery"] = out["uplift"] * frame["overdue_amount"].to_numpy()
        return out


def _fit_arm(frame: pd.DataFrame, seed: int) -> ArmModel:
    classifier = LGBMClassifier(random_state=seed, **LGBM_PARAMS)
    classifier.fit(frame[FEATURES], frame["cured_60"].astype(int))
    return ArmModel(classifier)


def train_uplift_model(
    train: pd.DataFrame, calibration: pd.DataFrame, seed: int = 0
) -> UpliftModel:
    """`train` / `calibration` are labelled episode frames (continuum.scoring.features),
    already split by time; calibration episodes must come after the training ones.
    """
    model = UpliftModel(
        self_cure=_fit_arm(train[~train["treated"]], seed),
        response=_fit_arm(train[train["treated"]], seed),
    )
    for arm, treated in ((model.self_cure, False), (model.response, True)):
        held_out = calibration[calibration["treated"] == treated]
        arm.calibrator = PlattCalibrator().fit(
            arm.raw(held_out), held_out["cured_60"].astype(int).to_numpy()
        )
    return model
