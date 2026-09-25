"""Tests for continuum.resolution.network — materialising entity_edge_candidate into
entity_edge. Requires a live Postgres with migrations applied (the view is created by
0002_entity_edge_candidate_view.py).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from continuum.db.models.entity import Entity, EntityEdge, EntityMember
from continuum.db.models.party import PartyRecord
from continuum.db.session import session_scope
from continuum.resolution.network import materialise_network

pytestmark = pytest.mark.integration

SINGLETON_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _entity_with_record(
    session, full_name: str, address_line: str, postal_code: str, phone: str
) -> uuid.UUID:
    record_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    session.add(
        PartyRecord(
            record_id=record_id,
            source_system="test",
            source_record_id=str(record_id),
            full_name=full_name,
            date_of_birth=date(1980, 1, 1),
            address_line=address_line,
            postal_code=postal_code,
            phone=phone,
        )
    )
    session.add(
        Entity(
            entity_id=entity_id,
            cluster_size=1,
            confidence=Decimal("1.0"),
            resolution_run_id=SINGLETON_RUN_ID,
        )
    )
    session.add(
        EntityMember(entity_id=entity_id, record_id=record_id, match_probability=Decimal("1.0"))
    )
    return entity_id


class TestMaterialiseNetwork:
    def test_shared_address_produces_an_edge(self) -> None:
        with session_scope() as session:
            entity_1 = _entity_with_record(session, "Person One", "1 Main St", "10001", "555-0001")
            entity_2 = _entity_with_record(session, "Person Two", "1 Main St", "10001", "555-0002")

        with session_scope() as session:
            materialise_network(session)

        with session_scope() as session:
            edges = session.scalars(
                select(EntityEdge).where(
                    EntityEdge.entity_id_a.in_([entity_1, entity_2]),
                    EntityEdge.entity_id_b.in_([entity_1, entity_2]),
                )
            ).all()
        assert any(e.edge_type.value == "shared_address" for e in edges)

    def test_unrelated_entities_produce_no_edge(self) -> None:
        with session_scope() as session:
            entity_1 = _entity_with_record(
                session, "Nobody Alike", "9 Oak Ave", "20002", "555-9001"
            )
            entity_2 = _entity_with_record(
                session, "Someone Else", "42 Pine Ln", "30003", "555-9002"
            )

        with session_scope() as session:
            materialise_network(session)

        with session_scope() as session:
            edges = session.scalars(
                select(EntityEdge).where(
                    EntityEdge.entity_id_a.in_([entity_1, entity_2]),
                    EntityEdge.entity_id_b.in_([entity_1, entity_2]),
                )
            ).all()
        assert edges == []

    def test_rerunning_is_idempotent_in_content(self) -> None:
        with session_scope() as session:
            entity_1 = _entity_with_record(session, "Repeat One", "5 Elm St", "40004", "555-1111")
            entity_2 = _entity_with_record(session, "Repeat Two", "5 Elm St", "40004", "555-2222")

        def edge_ids_for_pair(session) -> set[uuid.UUID]:
            rows = session.scalars(
                select(EntityEdge.edge_id).where(
                    EntityEdge.entity_id_a.in_([entity_1, entity_2]),
                    EntityEdge.entity_id_b.in_([entity_1, entity_2]),
                )
            ).all()
            return set(rows)

        with session_scope() as session:
            materialise_network(session)
        with session_scope() as session:
            first_run_edge_ids = edge_ids_for_pair(session)

        with session_scope() as session:
            materialise_network(session)
        with session_scope() as session:
            second_run_edge_ids = edge_ids_for_pair(session)

        assert first_run_edge_ids  # the shared-address edge from the first assertion's pair
        assert first_run_edge_ids == second_run_edge_ids
