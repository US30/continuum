"""Deterministic screening band logic — auto-confirm / auto-reject / adjudicate.

CLAUDE.md §8: "Band logic: auto-confirm above ceiling, auto-reject below floor, adjudicate
the middle." CLAUDE.md I5: screening is recall-first — the floor exists to guarantee zero
missed true hits on the labelled hard-name-pair set
(eval/golden/screening_hard_pairs.csv, scored by eval.screening_metrics), never tuned
toward precision. Pure function, no I/O — exhaustively testable per CLAUDE.md §10.
"""

from __future__ import annotations

from enum import StrEnum

from continuum.config.settings import Settings


class ScreeningBand(StrEnum):
    AUTO_REJECT = "auto_reject"
    ADJUDICATE = "adjudicate"
    AUTO_CONFIRM = "auto_confirm"


def band_for_score(score: float, settings: Settings) -> ScreeningBand:
    """Ceiling wins ties over floor when the two thresholds are ever misconfigured to
    overlap (ceiling <= floor) — auto-confirm is checked first, so a candidate is never
    silently auto-rejected when it should have been confirmed.
    """
    if score >= settings.screening_auto_confirm_ceiling:
        return ScreeningBand.AUTO_CONFIRM
    if score <= settings.screening_auto_reject_floor:
        return ScreeningBand.AUTO_REJECT
    return ScreeningBand.ADJUDICATE
