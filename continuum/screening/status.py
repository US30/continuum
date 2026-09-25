"""Whether an entity's sanctions/PEP screening is resolved — the guard's hard-block input.

CLAUDE.md §7: "hard block if risk file screening state unresolved". Derived, not stored:

  HIT         some current-snapshot decision for the entity is `hit`
  UNRESOLVED  the entity was never screened against the current snapshot, or it still has a
              candidate with no decision (e.g. deferred because the adjudicator was off)
  CLEAR       screened against the current snapshot and every candidate is decided `clear`

Decisions are keyed on (entity, snapshot, listing) exactly as continuum.screening.run writes
them, so this reads back what that run wrote. No LLM (I1).
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from continuum.db.models.screening import ScreeningDecision
from continuum.screening import candidates as candidates_mod
from continuum.screening.run import _decision_id


class ScreeningState(StrEnum):
    CLEAR = "clear"
    HIT = "hit"
    UNRESOLVED = "unresolved"


def screening_state(session: Session, entity_id: uuid.UUID) -> ScreeningState:
    snapshot_id = candidates_mod.current_snapshot_id(session)
    if snapshot_id is None:
        return ScreeningState.UNRESOLVED

    decisions = {
        row.decision_id: row.outcome
        for row in session.execute(
            select(ScreeningDecision.decision_id, ScreeningDecision.outcome).where(
                ScreeningDecision.entity_id == entity_id,
                ScreeningDecision.snapshot_id == snapshot_id,
            )
        )
    }
    if any(outcome == "hit" for outcome in decisions.values()):
        return ScreeningState.HIT
    if not decisions:
        return ScreeningState.UNRESOLVED

    for candidate in candidates_mod.generate_candidates(session, [entity_id]):
        decision_id = _decision_id(entity_id, snapshot_id, candidate.listing_id)
        if decision_id not in decisions:
            return ScreeningState.UNRESOLVED
    return ScreeningState.CLEAR
