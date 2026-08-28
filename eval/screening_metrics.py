"""Metric 1 — screening precision at 100% recall on the labelled hard-name-pair set.

CLAUDE.md §8 Phase 3 done-when: "precision at 100% recall on the labelled set... and the
proportion of candidates reaching the model is measured." CLAUDE.md §9: "you can never
miss a true hit, so false positives are the only lever."

Scored against continuum.screening.bands only, not the LLM adjudicator: a labelled pair is
"flagged" if band_for_score puts it in AUTO_CONFIRM or ADJUDICATE (i.e. it did not get
silently auto-rejected), "cleared" if AUTO_REJECT. This measures exactly what CLAUDE.md I5
asks the deterministic layer to guarantee — recall — and reports the proportion reaching
the model as its own separate number, matching the done-when's two-part phrasing. It
intentionally does not run continuum.screening.adjudicate: doing so would need a live LLM
this eval never calls, and would conflate "did the floor let a true hit through" with "did
the model call it correctly" — two different questions with two different owners (I5 vs
the adjudicator's own quality, which is not yet a tracked metric).

Run via `make screen-metrics` (`python -m eval.screening_metrics`). Uses the live Postgres
connection only to reach pg_trgm's `similarity()` function — no application tables are
read or written.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from continuum.config.settings import get_settings
from continuum.db.session import session_scope
from continuum.screening.bands import ScreeningBand, band_for_score

GOLDEN_SET_PATH = Path(__file__).parent / "golden" / "screening_hard_pairs.csv"


@dataclass(frozen=True)
class HardPair:
    case_id: str
    case_type: str
    query_name: str
    listing_name: str
    is_true_match: bool


@dataclass(frozen=True)
class ScreeningMetrics:
    true_positive: int
    false_negative: int
    flagged: int
    total: int

    @property
    def recall(self) -> float | None:
        denom = self.true_positive + self.false_negative
        return self.true_positive / denom if denom else None

    @property
    def precision(self) -> float | None:
        return self.true_positive / self.flagged if self.flagged else None

    @property
    def proportion_reaching_model(self) -> float:
        return self.flagged / self.total if self.total else 0.0


def load_golden_set(path: Path = GOLDEN_SET_PATH) -> list[HardPair]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return [
            HardPair(
                case_id=row["case_id"],
                case_type=row["case_type"],
                query_name=row["query_name"],
                listing_name=row["listing_name"],
                is_true_match=row["is_true_match"].strip().lower() == "true",
            )
            for row in reader
        ]


def _name_similarity(session: Session, name_a: str, name_b: str) -> float:
    row = session.execute(
        text("SELECT similarity(:a, :b)"), {"a": name_a, "b": name_b}
    ).first()
    return float(row[0])


def compute_screening_metrics(pairs: list[HardPair]) -> tuple[ScreeningMetrics, list[HardPair]]:
    settings = get_settings()
    tp = fn = flagged = 0
    missed: list[HardPair] = []

    with session_scope() as session:
        for pair in pairs:
            score = _name_similarity(session, pair.query_name, pair.listing_name)
            band = band_for_score(score, settings)
            is_flagged = band in (ScreeningBand.AUTO_CONFIRM, ScreeningBand.ADJUDICATE)

            if is_flagged:
                flagged += 1
            if pair.is_true_match:
                if is_flagged:
                    tp += 1
                else:
                    fn += 1
                    missed.append(pair)

    return ScreeningMetrics(true_positive=tp, false_negative=fn, flagged=flagged, total=len(pairs)), missed


def _format(value: float | None) -> str:
    return "n/a (no denominator)" if value is None else f"{value:.4f}"


def report() -> ScreeningMetrics:
    pairs = load_golden_set()
    metrics, missed = compute_screening_metrics(pairs)

    print("")
    print("=== CONTINUUM screening metrics (Metric 1: precision at 100% recall) ===")
    print(f"labelled pairs:               {metrics.total}")
    print(f"true matches flagged (TP):    {metrics.true_positive}")
    print(f"true matches missed (FN):     {metrics.false_negative}")
    print(f"recall:                       {_format(metrics.recall)}")
    print(f"precision (at this recall):   {_format(metrics.precision)}")
    print(f"proportion flagged overall:   {metrics.proportion_reaching_model:.4f}")
    if missed:
        print("MISSED true matches (I5 violation — floor must be lowered):")
        for pair in missed:
            print(f"  {pair.case_id} ({pair.case_type}): {pair.query_name!r} vs {pair.listing_name!r}")
    print("")
    return metrics


if __name__ == "__main__":
    report()
