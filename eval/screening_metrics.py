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
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from continuum.config.settings import get_settings
from continuum.db.session import session_scope
from continuum.screening.bands import ScreeningBand, band_for_score
from continuum.screening.candidates import ScreeningCandidate
from continuum.screening.schemas import AdjudicationVerdict

GOLDEN_SET_PATH = Path(__file__).parent / "golden" / "screening_hard_pairs.csv"


@dataclass(frozen=True)
class HardPair:
    case_id: str
    case_type: str
    query_name: str
    listing_name: str
    query_dob: date | None
    listing_dob: date | None
    is_true_match: bool


@dataclass(frozen=True)
class ScreeningMetrics:
    """Two stages, reported separately so the adjudicator's effect is visible.

    Stage 1 (deterministic bands): a pair is "flagged" unless auto-rejected.
    Stage 2 (optional, needs a live LLM): flagged pairs in the ADJUDICATE band are decided
    by the adjudicator; a "reject" verdict on a true match is a missed hit (I5).
    """

    true_positive: int
    false_negative: int
    flagged: int
    total: int
    auto_confirmed: int = 0
    adjudicated: int = 0
    # Stage 2; None until an adjudicator was actually run.
    final_true_positive: int | None = None
    final_false_negative: int | None = None
    final_positive: int | None = None

    @property
    def recall(self) -> float | None:
        denom = self.true_positive + self.false_negative
        return self.true_positive / denom if denom else None

    @property
    def precision(self) -> float | None:
        return self.true_positive / self.flagged if self.flagged else None

    @property
    def proportion_reaching_model(self) -> float:
        return self.adjudicated / self.total if self.total else 0.0

    @property
    def final_recall(self) -> float | None:
        if self.final_true_positive is None or self.final_false_negative is None:
            return None
        denom = self.final_true_positive + self.final_false_negative
        return self.final_true_positive / denom if denom else None

    @property
    def final_precision(self) -> float | None:
        if self.final_true_positive is None or not self.final_positive:
            return None
        return self.final_true_positive / self.final_positive


def _parse_date(value: str) -> date | None:
    return date.fromisoformat(value) if value.strip() else None


def load_golden_set(path: Path = GOLDEN_SET_PATH) -> list[HardPair]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return [
            HardPair(
                case_id=row["case_id"],
                case_type=row["case_type"],
                query_name=row["query_name"],
                listing_name=row["listing_name"],
                query_dob=_parse_date(row["query_dob"]),
                listing_dob=_parse_date(row["listing_dob"]),
                is_true_match=row["is_true_match"].strip().lower() == "true",
            )
            for row in reader
        ]


def _name_similarity(session: Session, name_a: str, name_b: str) -> float:
    row = session.execute(text("SELECT similarity(:a, :b)"), {"a": name_a, "b": name_b}).first()
    return float(row[0])


Adjudicator = Callable[[ScreeningCandidate], AdjudicationVerdict]


def _as_candidate(pair: HardPair, score: float) -> ScreeningCandidate:
    return ScreeningCandidate(
        entity_id=uuid.UUID(int=0),
        listing_id=pair.case_id,
        listing_primary_name=pair.listing_name,
        matched_alias=pair.listing_name,
        entity_name=pair.query_name,
        entity_date_of_birth=pair.query_dob,
        listing_birth_date=pair.listing_dob,
        listing_countries=None,
        listing_topics=None,
        name_similarity=score,
    )


def compute_screening_metrics(
    pairs: list[HardPair], adjudicator: Adjudicator | None = None
) -> tuple[ScreeningMetrics, list[HardPair]]:
    settings = get_settings()
    tp = fn = flagged = auto_confirmed = adjudicated = 0
    final_tp = final_fn = final_pos = 0
    missed: list[HardPair] = []

    with session_scope() as session:
        for pair in pairs:
            score = _name_similarity(session, pair.query_name, pair.listing_name)
            candidate = _as_candidate(pair, score)
            band = band_for_score(score, settings, dob_corroborated=candidate.dob_match)
            is_flagged = band in (ScreeningBand.AUTO_CONFIRM, ScreeningBand.ADJUDICATE)

            flagged += is_flagged
            auto_confirmed += band == ScreeningBand.AUTO_CONFIRM
            adjudicated += band == ScreeningBand.ADJUDICATE
            if pair.is_true_match:
                if is_flagged:
                    tp += 1
                else:
                    fn += 1
                    missed.append(pair)

            if adjudicator is not None:
                if band == ScreeningBand.AUTO_CONFIRM:
                    final_hit = True
                elif band == ScreeningBand.ADJUDICATE:
                    final_hit = adjudicator(candidate).verdict == "confirm"
                else:
                    final_hit = False
                final_pos += final_hit
                if pair.is_true_match:
                    final_tp += final_hit
                    final_fn += not final_hit

    return ScreeningMetrics(
        true_positive=tp,
        false_negative=fn,
        flagged=flagged,
        total=len(pairs),
        auto_confirmed=auto_confirmed,
        adjudicated=adjudicated,
        final_true_positive=final_tp if adjudicator else None,
        final_false_negative=final_fn if adjudicator else None,
        final_positive=final_pos if adjudicator else None,
    ), missed


def _format(value: float | None) -> str:
    return "n/a (no denominator)" if value is None else f"{value:.4f}"


def report(with_adjudicator: bool = False) -> ScreeningMetrics:
    pairs = load_golden_set()
    adjudicator: Adjudicator | None = None
    if with_adjudicator:
        from continuum.screening.adjudicate import adjudicate

        adjudicator = adjudicate
    metrics, missed = compute_screening_metrics(pairs, adjudicator)

    print("")
    print("=== CONTINUUM screening metrics (Metric 1: precision at 100% recall) ===")
    print(f"labelled pairs:                 {metrics.total}")
    print("-- stage 1: deterministic bands only --")
    print(f"auto-confirmed / adjudicate:    {metrics.auto_confirmed} / {metrics.adjudicated}")
    print(f"true matches flagged (TP):      {metrics.true_positive}")
    print(f"true matches missed (FN):       {metrics.false_negative}")
    print(f"recall:                         {_format(metrics.recall)}")
    print(f"precision (flagged = positive): {_format(metrics.precision)}")
    print(f"proportion reaching the model:  {metrics.proportion_reaching_model:.4f}")
    if with_adjudicator:
        print("-- stage 2: after LLM adjudicator --")
        print(
            f"final TP / FN:                  {metrics.final_true_positive} / "
            f"{metrics.final_false_negative}"
        )
        print(f"final recall:                   {_format(metrics.final_recall)}")
        print(f"final precision:                {_format(metrics.final_precision)}")
    else:
        print("-- stage 2 not run (needs a live LLM): rerun with --with-adjudicator --")
    if missed:
        print("MISSED true matches (I5 violation — floor must be lowered):")
        for pair in missed:
            print(
                f"  {pair.case_id} ({pair.case_type}): {pair.query_name!r} vs {pair.listing_name!r}"
            )
    print("")
    return metrics


if __name__ == "__main__":
    report(with_adjudicator="--with-adjudicator" in sys.argv)
