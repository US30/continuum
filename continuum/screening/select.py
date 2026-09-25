"""Selects which entities need (re-)screening this run.

CLAUDE.md §7 nightly batch: "entities with delinquency state change in last 24h + entities
matching newly added sanctions entries."

The second half is not yet fully implemented — flagged, not silently dropped:
continuum.foundation.sanctions fully replaces the sanctions snapshot on every load rather
than retaining the previous one, so there is nothing to diff against to detect "newly
added" listings specifically. Until sanctions snapshots are retained with a monotonic
version, this selects every entity that has *never* been screened at all (in addition to
delinquency-triggered ones), which still guarantees no entity goes permanently unscreened —
it just does not yet catch "existing, already-screened customer newly added to sanctions
on tonight's run" until the next delinquency-state change happens to also select them.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from continuum.db.models.account import Account, DelinquencyState
from continuum.db.models.entity import Entity
from continuum.db.models.screening import ScreeningDecision


def select_entities_for_screening(
    session: Session, since: datetime, *, full: bool = False
) -> list[uuid.UUID]:
    """`full=True` selects every entity — used to sweep up candidates that an earlier run
    deferred (adjudicator was off). Decisions are keyed deterministically, so re-deciding
    an already-decided candidate is a no-op (I6)."""
    if full:
        return sorted(session.scalars(select(Entity.entity_id)).all())

    changed_entities = session.scalars(
        select(Account.entity_id)
        .join(DelinquencyState, DelinquencyState.account_id == Account.account_id)
        .where(DelinquencyState.effective_from >= since)
        .distinct()
    ).all()

    never_screened = session.scalars(
        select(Entity.entity_id).where(Entity.entity_id.not_in(select(ScreeningDecision.entity_id)))
    ).all()

    return sorted(set(changed_entities) | set(never_screened))
