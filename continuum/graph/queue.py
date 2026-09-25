"""Queue-level helpers: pick which entities to run, and rank the resulting cases.

CLAUDE.md §7 prioritise: "rank by expected recovery net of contact cost". Each case's net
recovery is computed inside the graph; the *ranking across cases* happens here, after a batch,
because a rank only means something relative to the other cases in the queue.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from continuum.db.models.account import Account
from continuum.db.models.collections import Case, CaseDisposition
from continuum.db.models.entity import EntityEdge
from continuum.graph.nodes.case_assembly import DELINQUENT
from continuum.screening.status import ScreeningState, screening_state

BIG_CASE_OVERDUE = 5_000.0  # matches Settings.high_exposure_escalation_amount's intent


@dataclass(frozen=True)
class Pick:
    entity_id: uuid.UUID
    why: str


def pick_entities(session: Session, limit: int) -> list[Pick]:
    """A small, deliberately varied sample of delinquent entities, deterministic by id:
    multi-account (the collapse), network-linked, big-balance (human review), and ordinary."""
    rows = session.execute(
        select(Account.entity_id, func.count(), func.sum(Account.exposure))
        .where(Account.current_state.in_(DELINQUENT))
        .group_by(Account.entity_id)
        .order_by(Account.entity_id)
    ).all()
    linked = {
        e
        for a, b in session.execute(select(EntityEdge.entity_id_a, EntityEdge.entity_id_b))
        for e in (a, b)
    }
    buckets: dict[str, list[uuid.UUID]] = defaultdict(list)
    for entity_id, n_accounts, exposure in rows:
        clear = screening_state(session, entity_id) is ScreeningState.CLEAR
        if n_accounts > 1:
            buckets["multi-account" + (" clear" if clear else " unscreened")].append(entity_id)
        elif entity_id in linked:
            buckets["network-linked" + (" clear" if clear else " unscreened")].append(entity_id)
        elif clear and float(exposure) > BIG_CASE_OVERDUE:
            buckets["big balance clear"].append(entity_id)
        elif clear:
            buckets["ordinary clear"].append(entity_id)
        else:
            buckets["ordinary unscreened"].append(entity_id)

    order = [
        ("multi-account clear", 3),
        ("multi-account unscreened", 2),
        ("network-linked clear", 3),
        ("network-linked unscreened", 2),
        ("big balance clear", 3),
        ("ordinary clear", 5),
        ("ordinary unscreened", 2),
    ]
    picks: list[Pick] = []
    for name, quota in order:
        picks += [Pick(e, name) for e in buckets.get(name, [])[:quota]]
    return picks[:limit]


def rank_cases(session: Session) -> int:
    """Assign Case.rank 1..n by expected net recovery among cases still open for contact.
    Deterministic (ties broken by case_id), so re-ranking the same queue changes nothing."""
    cases = session.scalars(
        select(Case)
        .where(Case.disposition == CaseDisposition.OPEN, Case.expected_recovery.is_not(None))
        .order_by(Case.expected_recovery.desc(), Case.case_id)
    ).all()
    for rank, case in enumerate(cases, start=1):
        case.rank = rank
    return len(cases)
