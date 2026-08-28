"""Locks in the Phase 1 -> Phase 2 handoff invariant: before resolution has ever run, a
person's duplicate/hard-negative party_records must sit in *different* singleton entities
than their canonical record — never pre-merged via the in-memory-only person_id. If this
regresses, continuum.resolution (Phase 2) would have nothing left to discover on this
dataset. Requires a live Postgres — see docker/docker-compose.yml.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from continuum.db.models.entity import EntityMember
from continuum.db.models.eval import CollisionType, GroundTruthLink
from continuum.db.models.party import PartyRecord
from continuum.db.session import session_scope
from continuum.foundation.build import build

pytestmark = pytest.mark.integration


class TestSingletonEntitiesPreResolution:
    def test_duplicate_records_do_not_share_an_entity_with_their_canonical_record(self) -> None:
        build(n_persons=500, seed=13579)

        with session_scope() as session:
            same_person_links = session.scalars(
                select(GroundTruthLink).where(
                    GroundTruthLink.collision_type == CollisionType.SAME_PERSON
                )
            ).all()
            assert same_person_links, "expected at least one planted SAME_PERSON collision"

            entity_by_record = dict(
                session.execute(select(EntityMember.record_id, EntityMember.entity_id)).all()
            )

            for link in same_person_links:
                assert entity_by_record[link.record_id_a] != entity_by_record[link.record_id_b]

    def test_every_party_record_has_exactly_one_entity_member_row(self) -> None:
        build(n_persons=200, seed=13579)

        with session_scope() as session:
            n_records = session.scalar(select(func.count()).select_from(PartyRecord))
            n_memberships = session.scalar(select(func.count()).select_from(EntityMember))
            assert n_records == n_memberships
