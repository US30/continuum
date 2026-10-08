"""Read-only context a reviewer sees next to a parked case: the entity's accounts, its one-hop
network and its screening state. Plain SQL reads — no model call, no writes."""

from __future__ import annotations

import uuid

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from continuum.db.models.account import Account
from continuum.db.models.entity import EntityEdge
from continuum.screening.status import screening_state


class AccountRow(BaseModel):
    account_id: uuid.UUID
    product: str
    state: str
    exposure: float


class LinkRow(BaseModel):
    entity_id: uuid.UUID
    edge_type: str
    strength: float


class CaseFile(BaseModel):
    entity_id: uuid.UUID
    screening: str
    accounts: list[AccountRow]
    links: list[LinkRow]


def load_case_file(session: Session, entity_id: uuid.UUID) -> CaseFile:
    accounts = session.scalars(
        select(Account).where(Account.entity_id == entity_id).order_by(Account.account_id)
    ).all()
    edges = session.scalars(
        select(EntityEdge)
        .where(or_(EntityEdge.entity_id_a == entity_id, EntityEdge.entity_id_b == entity_id))
        .order_by(EntityEdge.strength.desc(), EntityEdge.edge_id)
    ).all()
    return CaseFile(
        entity_id=entity_id,
        screening=screening_state(session, entity_id).value,
        accounts=[
            AccountRow(
                account_id=a.account_id,
                product=a.product,
                state=a.current_state.value,
                exposure=float(a.exposure),
            )
            for a in accounts
        ],
        links=[
            LinkRow(
                entity_id=e.entity_id_b if e.entity_id_a == entity_id else e.entity_id_a,
                edge_type=e.edge_type.value,
                strength=float(e.strength),
            )
            for e in edges
        ],
    )
