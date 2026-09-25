"""Phase 4 entrypoint: build episodes, split by time, train, calibrate, report, save.

Run via `make train`. Uses only observable data — nothing from the eval-only truth file (I4);
scoring the *policy* against that truth is eval.uplift_metrics (`make uplift-metrics`).

Time separation (CLAUDE.md §8): labelled episodes are ordered by decision date and split into
train (earliest), calibration (next) and test (latest). Features were already built using
only information available on each episode's own decision date
(continuum.scoring.features), so there is no leakage in either direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import joblib
import pandas as pd
import typer
from sklearn.metrics import roc_auc_score

from continuum.config.settings import get_settings
from continuum.db.session import session_scope
from continuum.scoring import calibration
from continuum.scoring.features import load_episode_frame
from continuum.scoring.models import ArmModel, UpliftModel, train_uplift_model
from continuum.telemetry.logging import configure_logging, get_logger

app = typer.Typer(add_completion=False)
logger = get_logger(__name__)

MODEL_PATH = Path("data") / "models" / "uplift_v1.joblib"

# Chronological split fractions of the labelled episodes. Test is the remainder (25%).
TRAIN_FRACTION = 0.60
CALIBRATION_FRACTION = 0.15


@dataclass(frozen=True)
class TimeSplit:
    train: pd.DataFrame
    calibration: pd.DataFrame
    test: pd.DataFrame


def time_split(labelled: pd.DataFrame) -> TimeSplit:
    """Chronological split; ties on decision date never straddle a boundary."""
    ordered = labelled.sort_values(["decision_date", "account_id"]).reset_index(drop=True)
    dates = ordered["decision_date"]
    train_end = dates.iloc[int(len(ordered) * TRAIN_FRACTION) - 1]
    calibration_end = dates.iloc[int(len(ordered) * (TRAIN_FRACTION + CALIBRATION_FRACTION)) - 1]
    return TimeSplit(
        train=ordered[dates <= train_end],
        calibration=ordered[(dates > train_end) & (dates <= calibration_end)],
        test=ordered[dates > calibration_end],
    )


@dataclass(frozen=True)
class ArmReport:
    name: str
    n_test: int
    brier_raw: float
    brier_calibrated: float
    auc: float | None
    ece_calibrated: float
    reliability: list[calibration.ReliabilityBin]


def _arm_report(name: str, model_arm: ArmModel, test: pd.DataFrame) -> ArmReport:
    y = test["cured_60"].astype(int).to_numpy()
    raw = model_arm.raw(test)
    cal = model_arm.predict(test)
    bins = calibration.reliability_curve(y, cal)
    return ArmReport(
        name=name,
        n_test=len(test),
        brier_raw=calibration.brier(y, raw),
        brier_calibrated=calibration.brier(y, cal),
        auc=float(roc_auc_score(y, cal)) if len(set(y)) > 1 else None,
        ece_calibrated=calibration.expected_calibration_error(bins),
        reliability=bins,
    )


def evaluate_arms(model: UpliftModel, test: pd.DataFrame) -> list[ArmReport]:
    return [
        _arm_report("self-cure (no contact)", model.self_cure, test[~test["treated"]]),
        _arm_report("response (contacted)", model.response, test[test["treated"]]),
    ]


def train_from_database(as_of: date, seed: int = 0) -> tuple[UpliftModel, TimeSplit, pd.DataFrame]:
    with session_scope() as session:
        frame = load_episode_frame(session, as_of)
    labelled = frame[frame["labelled"]]
    split = time_split(labelled)
    model = train_uplift_model(split.train, split.calibration, seed=seed)
    return model, split, frame


def save_artifact(
    model: UpliftModel, split: TimeSplit, as_of: date, path: Path = MODEL_PATH
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "model": model,
            "as_of": as_of,
            "train_end": split.train["decision_date"].max(),
            "calibration_end": split.calibration["decision_date"].max(),
        },
        path,
    )


def load_artifact(path: Path = MODEL_PATH) -> dict[str, object]:
    loaded: dict[str, object] = joblib.load(path)
    return loaded


def print_report(split: TimeSplit, arms: list[ArmReport]) -> None:
    def span(df: pd.DataFrame) -> str:
        return f"{df['decision_date'].min()} .. {df['decision_date'].max()}"

    typer.echo("")
    typer.echo("=== CONTINUUM scoring: train / calibrate / test (time-separated) ===")
    for label, df in (
        ("train", split.train),
        ("calibration", split.calibration),
        ("test", split.test),
    ):
        typer.echo(
            f"{label:<12} n={len(df):<5} treated={df['treated'].mean():.2f} "
            f"cured={df['cured_60'].mean():.3f}  {span(df)}"
        )
    for arm in arms:
        auc = "n/a" if arm.auc is None else f"{arm.auc:.3f}"
        typer.echo("")
        typer.echo(f"-- {arm.name}: n_test={arm.n_test} --")
        typer.echo(f"Brier raw {arm.brier_raw:.4f} -> calibrated {arm.brier_calibrated:.4f}")
        typer.echo(f"AUC {auc}   ECE (calibrated) {arm.ece_calibrated:.4f}")
        typer.echo("reliability (predicted -> observed, n):")
        for b in arm.reliability:
            typer.echo(f"  {b.mean_predicted:.3f} -> {b.observed_rate:.3f}  ({b.count})")
    typer.echo("")


@app.command()
def main(seed: int = typer.Option(0, help="LightGBM seed.")) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    model, split, _ = train_from_database(settings.data_as_of_date, seed)
    save_artifact(model, split, settings.data_as_of_date)
    print_report(split, evaluate_arms(model, split.test))
    typer.echo(f"model saved: {MODEL_PATH}  ({model.version})")
    # Naive difference in observed cure rates is reported for contrast only — it is biased
    # because contact depends on exposure (confounding), which is why a T-learner is used.
    naive = split.test.groupby("treated")["cured_60"].mean()
    if len(naive) == 2:
        gap = naive[True] - naive[False]
        typer.echo(f"naive observed treated-minus-untreated cure rate (biased): {gap:+.3f}")


if __name__ == "__main__":
    app()
