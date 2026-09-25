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


def band_for_score(score: float, settings: Settings, *, dob_corroborated: bool) -> ScreeningBand:
    """Auto-confirm needs both a high name score *and* corroborating date of birth: a
    common name with a different or unknown DOB is exactly what the adjudicator is for,
    and confirming it automatically would hold a real customer (I2 blocks unresolved
    screening). Only the confirm side is gated — this can send more candidates to the
    adjudicator but never fewer to it, so recall is untouched (I5).

    Ceiling is checked before the floor, so a misconfigured floor >= ceiling can never
    silently auto-reject a high scorer.
    """
    if score >= settings.screening_auto_confirm_ceiling and dob_corroborated:
        return ScreeningBand.AUTO_CONFIRM
    if score >= settings.screening_auto_confirm_ceiling:
        return ScreeningBand.ADJUDICATE
    if score <= settings.screening_auto_reject_floor:
        return ScreeningBand.AUTO_REJECT
    return ScreeningBand.ADJUDICATE
