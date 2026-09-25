"""case_assembly — entity + every delinquent account + one-hop network + risk-file state.

CLAUDE.md §7: "linked delinquent accounts COLLAPSE INTO ONE CASE here <- the thesis".
I1: deterministic, no model call. Reads only; the single write is the idempotent `case` row.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from continuum.db.models.account import Account, DelinquencyBucket, PaymentEvent
from continuum.db.models.collections import Case, CaseDisposition, ContactEvent
from continuum.db.models.entity import EntityEdge, EntityMember
from continuum.db.models.party import PartyRecord
from continuum.graph.deps import GraphDeps
from continuum.graph.state import (
    AccountView,
    CaseState,
    CaseView,
    ContactPoints,
    LinkedEntity,
    Route,
)
from continuum.llm.untrusted import sanitize_person_name
from continuum.screening.status import screening_state
from continuum.telemetry.spans import node_span

CASE_ID_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-0000000000c5")
DELINQUENT = (DelinquencyBucket.DPD_30, DelinquencyBucket.DPD_60, DelinquencyBucket.DPD_90)
RECENT_CONTACT_DAYS = 7


def case_id_for(entity_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(CASE_ID_NAMESPACE, str(entity_id))


def _overdue_by_account(
    session: Session, account_ids: list[uuid.UUID], as_of: date
) -> dict[uuid.UUID, tuple[float, int]]:
    """(overdue amount, days past due) per account as of `as_of`, from installment history."""
    rows = session.execute(
        select(
            PaymentEvent.account_id,
            func.sum(PaymentEvent.amount_due),
            func.min(PaymentEvent.due_date),
        )
        .where(
            PaymentEvent.account_id.in_(account_ids),
            PaymentEvent.due_date <= as_of,
            or_(
                PaymentEvent.paid_date.is_(None),
                PaymentEvent.paid_date > as_of,
                PaymentEvent.amount_paid < PaymentEvent.amount_due,
            ),
        )
        .group_by(PaymentEvent.account_id)
    ).all()
    return {account_id: (float(total), (as_of - oldest).days) for account_id, total, oldest in rows}


def _linked_entities(session: Session, entity_id: uuid.UUID) -> list[LinkedEntity]:
    edges = session.scalars(
        select(EntityEdge).where(
            or_(EntityEdge.entity_id_a == entity_id, EntityEdge.entity_id_b == entity_id)
        )
    ).all()
    other_ids = {e.entity_id_b if e.entity_id_a == entity_id else e.entity_id_a for e in edges}
    delinquent_others = set(
        session.scalars(
            select(Account.entity_id).where(
                Account.entity_id.in_(other_ids), Account.current_state.in_(DELINQUENT)
            )
        ).all()
    )
    return [
        LinkedEntity(
            entity_id=other,
            edge_type=e.edge_type.value,
            strength=float(e.strength),
            delinquent=other in delinquent_others,
        )
        for e in edges
        for other in [e.entity_id_b if e.entity_id_a == entity_id else e.entity_id_a]
    ]


def _first_name(full_name: str) -> str:
    """Validate the WHOLE stored name, then take its first word. Checking only the first word
    would let the opening token of `Ignore previous instructions...` through as a "name"."""
    validated = sanitize_person_name(full_name, fallback="", max_words=6, max_len=80)
    return validated.split()[0] if validated else "there"


def _contact_points(session: Session, entity_id: uuid.UUID) -> ContactPoints:
    records = session.scalars(
        select(PartyRecord)
        .join(EntityMember, EntityMember.record_id == PartyRecord.record_id)
        .where(EntityMember.entity_id == entity_id)
    ).all()
    canonical = min(records, key=lambda r: (r.source_system != "loan_origination", r.full_name))
    return ContactPoints(
        first_name=_first_name(canonical.full_name),
        has_phone=any(r.phone for r in records),
        has_email=any(r.email for r in records),
    )


def _recent_contacts(session: Session, entity_id: uuid.UUID, as_of: date) -> int:
    end = datetime.combine(as_of + timedelta(days=1), time.min)
    start = end - timedelta(days=RECENT_CONTACT_DAYS)
    return int(
        session.scalar(
            select(func.count())
            .select_from(ContactEvent)
            .where(
                ContactEvent.entity_id == entity_id,
                ContactEvent.sent_at >= start,
                ContactEvent.sent_at < end,
            )
        )
        or 0
    )


def load_case(session: Session, entity_id: uuid.UUID, as_of: date) -> CaseView | None:
    """None if the entity has no delinquent account."""
    accounts = session.scalars(
        select(Account)
        .where(Account.entity_id == entity_id, Account.current_state.in_(DELINQUENT))
        .order_by(Account.account_id)
    ).all()
    if not accounts:
        return None

    overdue = _overdue_by_account(session, [a.account_id for a in accounts], as_of)
    views = [
        AccountView(
            account_id=a.account_id,
            product=a.product,
            exposure=float(a.exposure),
            state=a.current_state.value,
            days_past_due=overdue.get(a.account_id, (0.0, 0))[1],
            overdue_amount=overdue.get(a.account_id, (0.0, 0))[0],
        )
        for a in accounts
    ]
    return CaseView(
        entity_id=entity_id,
        case_id=case_id_for(entity_id),
        as_of=as_of,
        accounts=views,
        linked_entities=_linked_entities(session, entity_id),
        screening=screening_state(session, entity_id),
        contact=_contact_points(session, entity_id),
        recent_contacts_7d=_recent_contacts(session, entity_id, as_of),
    )


def _upsert_case_row(session: Session, case: CaseView) -> None:
    row = session.get(Case, case.case_id)
    if row is None:
        session.add(
            Case(case_id=case.case_id, entity_id=case.entity_id, disposition=CaseDisposition.OPEN)
        )


def assemble_case(state: CaseState, deps: GraphDeps) -> dict[str, Any]:
    with node_span("case_assembly", state.entity_id), deps.session_scope() as session:
        case = load_case(session, state.entity_id, state.as_of)
        if case is None:
            return {
                "route": Route(
                    route="no_delinquency",
                    disposition=CaseDisposition.OPEN.value,
                    note="entity has no delinquent account",
                ),
                "events": ["case_assembly: no delinquent account"],
            }
        _upsert_case_row(session, case)
        return {
            "case": case,
            "events": [
                f"case_assembly: {len(case.accounts)} account(s) -> 1 case, "
                f"{len(case.linked_entities)} linked, screening={case.screening.value}"
            ],
        }
