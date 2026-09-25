"""Score currently delinquent accounts with the trained uplift model, for the decisioning graph.

Scores every episode once (feature building is ~0.4 s for the whole book) and serves lookups.
CLAUDE.md I1: no LLM here. Caveat carried into the graph's rationale: the model was trained on
30-DPD decision-date episodes, so its uplift for an account that is already 60/90 DPD is an
extrapolation from its decision-date features.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from continuum.db.session import session_scope
from continuum.scoring.features import load_episode_frame
from continuum.scoring.models import UpliftModel
from continuum.scoring.train import MODEL_PATH, load_artifact


@dataclass(frozen=True)
class AccountScore:
    p_cure_no_contact: float
    p_cure_contact: float
    uplift: float
    overdue_amount: float

    @property
    def expected_incremental_recovery(self) -> float:
        return self.uplift * self.overdue_amount


class Scorer:
    def __init__(self, model: UpliftModel, frame: pd.DataFrame) -> None:
        self.model_version = model.version
        scores = model.score(frame)
        self._by_account: dict[str, AccountScore] = {
            str(account_id): AccountScore(p0, p1, uplift, float(overdue))
            for account_id, p0, p1, uplift, overdue in zip(
                scores["account_id"],
                scores["p_cure_no_contact"],
                scores["p_cure_contact"],
                scores["uplift"],
                frame["overdue_amount"],
                strict=True,
            )
        }

    @classmethod
    def from_database(cls, as_of: date, path: Path = MODEL_PATH) -> Scorer:
        artifact = load_artifact(path)
        model = artifact["model"]
        assert isinstance(model, UpliftModel)
        with session_scope() as session:
            frame = load_episode_frame(session, as_of)
        return cls(model, frame)

    def score(self, account_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, AccountScore]:
        """Accounts with no episode (never reached 30 DPD) are simply absent."""
        return {
            account_id: self._by_account[str(account_id)]
            for account_id in account_ids
            if str(account_id) in self._by_account
        }
