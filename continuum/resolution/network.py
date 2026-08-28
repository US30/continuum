"""Materialises the one-hop network: entity_edge_candidate view -> entity_edge table.

CLAUDE.md §6: entity_edge is "materialised nightly." Every run fully replaces the table's
contents from the view inside one transaction (delete + insert, same atomic-swap pattern as
continuum.foundation.sanctions) — a person's address or phone changing should make a stale
edge disappear, not linger forever. Idempotent (CLAUDE.md I6): unchanged input data
produces byte-identical entity_edge rows, since edge_id is derived from (entity_id_a,
entity_id_b, edge_type) rather than assigned by insertion order.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from continuum.db.models.entity import EntityEdge

EDGE_ID_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-0000000000e6")


def _edge_id(entity_id_a: uuid.UUID, entity_id_b: uuid.UUID, edge_type: str) -> uuid.UUID:
    return uuid.uuid5(EDGE_ID_NAMESPACE, f"{entity_id_a}:{entity_id_b}:{edge_type}")


def materialise_network(session: Session) -> int:
    """Returns the number of edges written."""
    candidates = session.execute(
        text(
            "SELECT entity_id_a, entity_id_b, edge_type, strength FROM entity_edge_candidate"
        )
    ).all()

    session.execute(delete(EntityEdge))

    for entity_id_a, entity_id_b, edge_type, strength in candidates:
        stmt = pg_insert(EntityEdge).values(
            edge_id=_edge_id(entity_id_a, entity_id_b, edge_type),
            entity_id_a=entity_id_a,
            entity_id_b=entity_id_b,
            edge_type=edge_type,
            strength=strength,
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["edge_id"])
        session.execute(stmt)

    return len(candidates)
