"""End to end: generator -> database -> episode frame. The labels derived from payments in
the database must equal the generator's own latent draw for every labelled episode — the
strongest check that the feature pipeline and the delinquency machinery agree."""

from __future__ import annotations

import pytest

from continuum.config.settings import get_settings
from continuum.db.session import session_scope
from continuum.foundation.build import build
from continuum.foundation.generate import generate_population
from continuum.scoring.features import FEATURES, load_episode_frame

pytestmark = pytest.mark.integration

SEED = 24680
N = 600


def test_frame_labels_match_the_generators_latent_outcomes() -> None:
    build(n_persons=N, seed=SEED)
    truth = {
        str(a.account_id): a.episode
        for p in generate_population(N, SEED)
        for a in p.accounts
        if a.episode is not None
    }

    with session_scope() as session:
        frame = load_episode_frame(session, get_settings().data_as_of_date)
    frame = frame[frame["account_id"].astype(str).isin(truth)]

    assert len(frame) == len(truth)
    labelled = frame[frame["labelled"]]
    assert len(labelled) > 200
    for row in labelled.itertuples():
        ep = truth[str(row.account_id)]
        assert bool(row.cured_60) == ep.cured_60
        assert bool(row.treated) == ep.treated
        assert row.decision_date == ep.decision_date
        assert row.overdue_amount == pytest.approx(float(ep.overdue_amount))


def test_features_have_no_missing_values() -> None:
    with session_scope() as session:
        frame = load_episode_frame(session, get_settings().data_as_of_date)
    assert not frame[FEATURES].isna().any().any()
