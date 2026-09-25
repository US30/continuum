"""Resolution precision/recall/F1 against ground_truth_link.

CLAUDE.md §8 Phase 2 done-when: "resolution precision/recall/F1 against ground_truth_link
is reported by a single command." Run via `make resolve-metrics`
(`python -m eval.resolution_metrics`).

CLAUDE.md I4: ground_truth_link is eval-only — this module, under eval/, is exactly the
"evaluation harness" the invariant carves out; no continuum/ package outside
continuum.foundation.collisions (the writer) may import it (enforced by
tests/test_invariants.py).

Definitions, evaluated per planted pair in ground_truth_link (not over the whole
population — only planted pairs have a label at all):
    TP - a SAME_PERSON pair that ended up in the same entity (correctly merged)
    FN - a SAME_PERSON pair that did not (a missed collision)
    FP - a HARD_NEGATIVE pair that ended up in the same entity (incorrectly merged)
    TN - a HARD_NEGATIVE pair that did not (correctly kept apart)
precision = TP / (TP + FP), recall = TP / (TP + FN), F1 = harmonic mean of the two.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from continuum.db.models.entity import EntityMember
from continuum.db.models.eval import CollisionType, GroundTruthLink
from continuum.db.session import session_scope
from continuum.telemetry.logging import configure_logging, get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ResolutionMetrics:
    true_positive: int
    false_negative: int
    false_positive: int
    true_negative: int

    @property
    def precision(self) -> float | None:
        denom = self.true_positive + self.false_positive
        return self.true_positive / denom if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.true_positive + self.false_negative
        return self.true_positive / denom if denom else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)


def compute_resolution_metrics(session: Session) -> ResolutionMetrics:
    entity_by_record = dict(
        session.execute(select(EntityMember.record_id, EntityMember.entity_id)).all()
    )
    links = session.scalars(select(GroundTruthLink)).all()

    tp = fn = fp = tn = 0
    for link in links:
        entity_a = entity_by_record.get(link.record_id_a)
        entity_b = entity_by_record.get(link.record_id_b)
        same_entity = entity_a is not None and entity_a == entity_b

        if link.collision_type == CollisionType.SAME_PERSON:
            tp += same_entity
            fn += not same_entity
        else:  # HARD_NEGATIVE
            fp += same_entity
            tn += not same_entity

    return ResolutionMetrics(
        true_positive=tp, false_negative=fn, false_positive=fp, true_negative=tn
    )


def _format(value: float | None) -> str:
    return "n/a (no denominator)" if value is None else f"{value:.4f}"


def report() -> ResolutionMetrics:
    with session_scope() as session:
        metrics = compute_resolution_metrics(session)

    print("")
    print("=== CONTINUUM resolution metrics (vs. ground_truth_link) ===")
    print(
        f"TP={metrics.true_positive}  FN={metrics.false_negative}  "
        f"FP={metrics.false_positive}  TN={metrics.true_negative}"
    )
    print(f"precision: {_format(metrics.precision)}")
    print(f"recall:    {_format(metrics.recall)}")
    print(f"F1:        {_format(metrics.f1)}")
    print("")
    return metrics


if __name__ == "__main__":
    configure_logging()
    report()
