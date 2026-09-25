from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from continuum.scoring.train import CALIBRATION_FRACTION, TRAIN_FRACTION, time_split


def _labelled(n: int = 400, repeats: int = 3) -> pd.DataFrame:
    days = [date(2025, 1, 1) + timedelta(days=i // repeats) for i in range(n)]
    return pd.DataFrame({"account_id": [f"a{i:04d}" for i in range(n)], "decision_date": days})


def test_split_is_strictly_chronological_with_no_overlap() -> None:
    split = time_split(_labelled())
    assert split.train["decision_date"].max() < split.calibration["decision_date"].min()
    assert split.calibration["decision_date"].max() < split.test["decision_date"].min()


def test_split_partitions_every_episode_exactly_once() -> None:
    frame = _labelled()
    split = time_split(frame)
    ids = pd.concat([split.train, split.calibration, split.test])["account_id"]
    assert sorted(ids) == sorted(frame["account_id"])


def test_split_sizes_are_close_to_the_configured_fractions() -> None:
    split = time_split(_labelled(1_000))
    assert abs(len(split.train) / 1_000 - TRAIN_FRACTION) < 0.02
    assert abs(len(split.calibration) / 1_000 - CALIBRATION_FRACTION) < 0.02


def test_input_order_does_not_matter() -> None:
    frame = _labelled()
    shuffled = frame.sample(frac=1.0, random_state=1)
    a, b = time_split(frame), time_split(shuffled)
    assert list(a.test["account_id"]) == list(b.test["account_id"])
