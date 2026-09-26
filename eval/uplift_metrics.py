"""Metric 2 — incremental recovery from uplift ranking vs the rules baseline, in currency.

CLAUDE.md §9: "Incremental recovery from uplift ranking vs. the rules baseline — in currency,
not AUC." CLAUDE.md §8 Phase 4 done-when: reported "with the baseline definition written
down". Run via `make uplift-metrics` (`python -m eval.uplift_metrics`).

BASELINE DEFINITION (the rules policy, CLAUDE.md §8 Phase 1): on the day an account reaches
30 DPD, contact it with probability min(0.9, 0.1 + exposure / 20,000) — contact is weighted
by exposure. Its realised choices on the test episodes are `realised_rules`. Every policy
below is given the same contact budget K = the number of contacts the rules policy actually
made on the test episodes, so the comparison is about *whom* to contact, not how many.

Policies (all evaluated on the time-held-out test episodes, never on training data):
    rules_policy        the rules policy in expectation (each account contacted with its
                        propensity e_i)                                        <- BASELINE
    realised_rules      the one draw of that policy that actually happened (oracle only:
                        selecting on the realised treatment makes an IPW estimate invalid)
    rules_top_k         the K largest-exposure accounts (a deterministic rules variant)
    uplift_top_k        the K with the highest predicted expected incremental recovery
    random_k            K chosen at random (expected value)
    oracle_top_k        the K with the highest TRUE expected incremental recovery (ceiling)

VALUE of contacting an episode = tau_i * overdue_amount_i, where tau_i = P(cure | contact) -
P(cure | no contact) is the true effect: the expected currency *added* by the contact
(recovery that would not have happened anyway). A policy's value is the sum over its
contacts. METRIC 2 = value(uplift_top_k) - value(rules_policy), in currency.

Two estimators, reported side by side:
    oracle  uses the true tau from data/true_uplift.csv (eval-only, CLAUDE.md I4). Exact on
            this synthetic data; impossible on real data.
    IPW     uses only observed outcomes, weighting by the known contact propensity
            e_i = min(0.9, 0.1 + exposure / 20,000). What one could actually do in production;
            unbiased but noisy at this sample size.

This is the only place, besides continuum.foundation, that reads the truth file.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import typer
from scipy.stats import spearmanr

from continuum.config.settings import get_settings
from continuum.scoring.train import load_artifact, train_from_database

TRUE_UPLIFT_PATH = Path("data") / "true_uplift.csv"
N_BOOTSTRAP = 1_000
BOOTSTRAP_SEED = 7


def load_truth(path: Path = TRUE_UPLIFT_PATH) -> pd.DataFrame:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    truth = pd.DataFrame(rows)
    truth["account_id"] = truth["account_id"].astype(str)
    for col in ("p_cure_control", "p_cure_treated", "overdue_amount"):
        truth[col] = truth[col].astype(float)
    truth["cured_60_truth"] = truth["cured_60"].astype(int).astype(bool)
    truth["tau"] = truth["p_cure_treated"] - truth["p_cure_control"]
    return truth[["account_id", "tau", "cured_60_truth", "overdue_amount"]].rename(
        columns={"overdue_amount": "overdue_amount_truth"}
    )


def contact_propensity(exposure: np.ndarray) -> np.ndarray:
    """The rules policy's known P(contact | exposure) — see the baseline definition above."""
    return np.minimum(0.9, 0.1 + exposure / 20_000)


@dataclass(frozen=True)
class PolicyValue:
    name: str
    n_contacted: int
    oracle_value: float
    ipw_value: float


@dataclass(frozen=True)
class UpliftReport:
    n_test: int
    budget: int
    policies: list[PolicyValue]
    metric2_oracle: float
    metric2_oracle_ci: tuple[float, float]
    metric2_ipw: float
    metric2_ipw_ci: tuple[float, float]
    metric2_vs_rules_top_k: float
    spearman_tau: float
    top_quintile_true_tau: float
    bottom_quintile_true_tau: float
    label_mismatches: int


def _top_k(values: np.ndarray, k: int) -> np.ndarray:
    selected = np.zeros(len(values), dtype=bool)
    selected[np.argsort(-values, kind="stable")[:k]] = True
    return selected


def evaluate_policies(
    test: pd.DataFrame, scores: pd.DataFrame, truth: pd.DataFrame
) -> UpliftReport:
    df = (
        test.merge(scores, on="account_id")
        .assign(account_id=lambda d: d["account_id"].astype(str))
        .merge(truth, on="account_id", how="left")
    )
    if df["tau"].isna().any():
        raise ValueError("test episodes missing from the truth file — rebuild the dataset")
    label_mismatches = int((df["cured_60"].astype(bool) != df["cured_60_truth"]).sum())

    amount = df["overdue_amount"].to_numpy(dtype=float)
    tau = df["tau"].to_numpy(dtype=float)
    value_if_contacted = tau * amount  # oracle expected currency added by contacting i
    treated = df["treated"].to_numpy(dtype=bool)
    y_amount = df["recovered_amount"].to_numpy(dtype=float)  # observed recovery
    e = contact_propensity(np.exp(df["log_exposure"].to_numpy(dtype=float)))
    # IPW estimate of the incremental recovery from contacting i (zero unless selected).
    ipw_gain = (treated / e - (~treated) / (1 - e)) * y_amount

    budget = int(treated.sum())
    rules_expected = e  # contact probability of each account under the rules policy
    weights = {
        "rules_policy (BASELINE)": rules_expected,
        "realised_rules (one draw)": treated.astype(float),
        "rules_top_k (by exposure)": _top_k(
            df["log_exposure"].to_numpy(dtype=float), budget
        ).astype(float),
        "uplift_top_k (model)": _top_k(
            df["expected_incremental_recovery"].to_numpy(), budget
        ).astype(float),
        "oracle_top_k (ceiling)": _top_k(value_if_contacted, budget).astype(float),
        "random_k (expected)": np.full(len(df), budget / len(df)),
    }
    policies = [
        PolicyValue(
            name,
            round(float(w.sum())),
            float((w * value_if_contacted).sum()),
            # A draw selected on the realised treatment cannot be IPW-evaluated.
            float("nan") if name.startswith("realised") else float((w * ipw_gain).sum()),
        )
        for name, w in weights.items()
    ]
    by_name = {p.name: p for p in policies}
    baseline = by_name["rules_policy (BASELINE)"]
    model = by_name["uplift_top_k (model)"]

    diff_vector = (weights["uplift_top_k (model)"] - weights["rules_policy (BASELINE)"]) * (
        value_if_contacted
    )
    ipw_diff_vector = (weights["uplift_top_k (model)"] - weights["rules_policy (BASELINE)"]) * (
        ipw_gain
    )
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    resamples = [rng.integers(0, len(df), len(df)) for _ in range(N_BOOTSTRAP)]
    boot = [diff_vector[idx].sum() for idx in resamples]
    ipw_boot = [ipw_diff_vector[idx].sum() for idx in resamples]

    quintile = max(len(df) // 5, 1)
    order = np.argsort(-df["uplift"].to_numpy())
    return UpliftReport(
        n_test=len(df),
        budget=budget,
        policies=policies,
        metric2_oracle=model.oracle_value - baseline.oracle_value,
        metric2_oracle_ci=(float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))),
        metric2_ipw=model.ipw_value - baseline.ipw_value,
        metric2_ipw_ci=(float(np.percentile(ipw_boot, 2.5)), float(np.percentile(ipw_boot, 97.5))),
        metric2_vs_rules_top_k=model.oracle_value
        - by_name["rules_top_k (by exposure)"].oracle_value,
        spearman_tau=float(spearmanr(df["uplift"], tau).statistic),
        top_quintile_true_tau=float(tau[order[:quintile]].mean()),
        bottom_quintile_true_tau=float(tau[order[-quintile:]].mean()),
        label_mismatches=label_mismatches,
    )


def print_report(report: UpliftReport) -> None:
    typer.echo("")
    typer.echo("=== CONTINUUM Metric 2: incremental recovery, uplift ranking vs rules baseline ===")
    typer.echo(f"test episodes: {report.n_test}   contact budget K: {report.budget}")
    typer.echo("K = contacts the rules policy actually made; value = currency ADDED by contacts")
    typer.echo(f"{'policy':<28}{'contacts':>9}{'oracle $':>12}{'IPW $':>12}")
    for p in report.policies:
        typer.echo(f"{p.name:<28}{p.n_contacted:>9}{p.oracle_value:>12,.0f}{p.ipw_value:>12,.0f}")
    lo, hi = report.metric2_oracle_ci
    typer.echo("")
    typer.echo(
        f"METRIC 2  uplift_top_k minus baseline, oracle:  {report.metric2_oracle:+,.0f}  "
        f"(95% bootstrap CI {lo:+,.0f} .. {hi:+,.0f})"
    )
    ilo, ihi = report.metric2_ipw_ci
    typer.echo(
        f"          same, IPW (observed data only, noisy):  {report.metric2_ipw:+,.0f}  "
        f"(95% CI {ilo:+,.0f} .. {ihi:+,.0f})"
    )
    typer.echo(
        f"          uplift_top_k minus rules_top_k (oracle): {report.metric2_vs_rules_top_k:+,.0f}"
    )
    typer.echo("")
    typer.echo("uplift-model quality vs the truth:")
    typer.echo(f"  Spearman(predicted uplift, true tau): {report.spearman_tau:+.3f}")
    typer.echo(
        f"  mean true tau, top predicted quintile: {report.top_quintile_true_tau:+.3f}"
        f"   bottom: {report.bottom_quintile_true_tau:+.3f}"
    )
    typer.echo(f"  label/truth mismatches (data integrity, expect 0): {report.label_mismatches}")
    typer.echo("")


def compute() -> UpliftReport:
    settings = get_settings()
    artifact = load_artifact()
    _, split, _ = train_from_database(settings.data_as_of_date)  # same deterministic split
    model = artifact["model"]
    test = split.test
    scores = model.score(test)  # type: ignore[attr-defined]
    return evaluate_policies(test, scores, load_truth())


def report() -> UpliftReport:
    out = compute()
    print_report(out)
    return out


if __name__ == "__main__":
    report()
