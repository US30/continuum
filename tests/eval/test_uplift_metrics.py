"""Metric 2 harness on tiny hand-built episodes where the right answer is known."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from eval.uplift_metrics import _top_k, contact_propensity, evaluate_policies


def _inputs(n: int = 200, seed: int = 0):
    rng = np.random.default_rng(seed)
    log_exposure = rng.normal(8.5, 0.6, n)
    tau = rng.uniform(-0.1, 0.5, n)
    amount = np.exp(log_exposure) / 10
    ids = [f"a{i}" for i in range(n)]
    e = contact_propensity(np.exp(log_exposure))
    treated = rng.random(n) < e
    cured = rng.random(n) < 0.4 + tau * treated
    test = pd.DataFrame(
        {
            "account_id": ids,
            "log_exposure": log_exposure,
            "treated": treated,
            "cured_60": cured,
            "overdue_amount": amount,
            "recovered_amount": amount * cured,
        }
    )
    truth = pd.DataFrame(
        {
            "account_id": ids,
            "tau": tau,
            "cured_60_truth": cured,
            "overdue_amount_truth": amount,
        }
    )

    def scores(predicted_tau: np.ndarray) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "account_id": ids,
                "uplift": predicted_tau,
                "expected_incremental_recovery": predicted_tau * amount,
            }
        )

    return test, truth, scores, tau


def test_top_k_selects_exactly_k_largest() -> None:
    selected = _top_k(np.array([3.0, 9.0, 1.0, 7.0]), 2)
    assert list(selected) == [False, True, False, True]


def test_contact_propensity_matches_the_rules_policy_and_is_bounded() -> None:
    p = contact_propensity(np.array([100.0, 10_000.0, 1e9]))
    assert p[0] == pytest.approx(0.105) and p[1] == pytest.approx(0.6) and p[2] == 0.9


def test_a_perfect_model_matches_the_oracle_ceiling() -> None:
    test, truth, scores, tau = _inputs()
    report = evaluate_policies(test, scores(tau), truth)
    by = {p.name: p for p in report.policies}
    assert by["uplift_top_k (model)"].oracle_value == pytest.approx(
        by["oracle_top_k (ceiling)"].oracle_value
    )
    assert report.metric2_oracle > 0
    assert report.spearman_tau == pytest.approx(1.0)


def test_a_random_model_does_not_beat_the_ceiling_and_every_policy_uses_the_same_budget() -> None:
    test, truth, scores, tau = _inputs()
    noise = np.random.default_rng(9).normal(size=len(tau))
    report = evaluate_policies(test, scores(noise), truth)
    by = {p.name: p for p in report.policies}
    assert by["uplift_top_k (model)"].oracle_value <= by["oracle_top_k (ceiling)"].oracle_value
    budget = report.budget
    for name in ("rules_top_k (by exposure)", "uplift_top_k (model)", "oracle_top_k (ceiling)"):
        assert by[name].n_contacted == budget
    assert budget == int(test["treated"].sum())


def test_metric2_is_model_minus_baseline_in_currency() -> None:
    test, truth, scores, tau = _inputs()
    report = evaluate_policies(test, scores(tau), truth)
    by = {p.name: p for p in report.policies}
    assert report.metric2_oracle == pytest.approx(
        by["uplift_top_k (model)"].oracle_value - by["rules_policy (BASELINE)"].oracle_value
    )
    low, high = report.metric2_oracle_ci
    assert low <= report.metric2_oracle <= high


def test_realised_draw_is_not_ipw_evaluated() -> None:
    test, truth, scores, tau = _inputs()
    by = {p.name: p for p in evaluate_policies(test, scores(tau), truth).policies}
    assert np.isnan(by["realised_rules (one draw)"].ipw_value)


def test_missing_truth_is_an_error_not_a_silent_drop() -> None:
    test, truth, scores, tau = _inputs()
    with pytest.raises(ValueError, match="truth"):
        evaluate_policies(test, scores(tau), truth.iloc[:-5])


def test_label_mismatches_are_counted() -> None:
    test, truth, scores, tau = _inputs()
    truth.loc[0, "cured_60_truth"] = not bool(test.loc[0, "cured_60"])
    assert evaluate_policies(test, scores(tau), truth).label_mismatches == 1
