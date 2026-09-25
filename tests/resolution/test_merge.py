"""Tests for continuum.resolution.merge — repointing Phase-1 singletons into a resolved
cluster. Builds a small hand-crafted scenario directly (not via continuum.foundation.build)
to keep the fixture minimal and the assertions exact. Requires a live Postgres.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from continuum.db.models.account import Account, DelinquencyBucket
from continuum.db.models.entity import Entity, EntityMember
from continuum.db.models.party import PartyRecord
from continuum.db.session import session_scope
from continuum.resolution.cluster import ResolvedCluster, stable_cluster_id
from continuum.resolution.merge import apply_resolution

pytestmark = pytest.mark.integration


def _make_party_record(session, record_id: uuid.UUID, name: str) -> None:
    session.add(
        PartyRecord(
            record_id=record_id,
            source_system="test",
            source_record_id=str(record_id),
            full_name=name,
            date_of_birth=date(1980, 1, 1),
        )
    )
    session.flush()


def _make_singleton(session, record_id: uuid.UUID) -> uuid.UUID:
    entity_id = uuid.uuid5(record_id, "phase1-singleton")
    session.add(
        Entity(
            entity_id=entity_id,
            cluster_size=1,
            confidence=Decimal("1.0"),
            resolution_run_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        )
    )
    session.flush()
    session.add(
        EntityMember(entity_id=entity_id, record_id=record_id, match_probability=Decimal("1.0"))
    )
    session.flush()
    return entity_id


class TestApplyResolution:
    def test_merging_two_singletons_repoints_account_and_deletes_absorbed_entity(self) -> None:
        record_a, record_b = uuid.uuid4(), uuid.uuid4()

        with session_scope() as session:
            _make_party_record(session, record_a, "Aldric Vantessen")
            _make_party_record(session, record_b, "Al Vantessen")
            entity_a = _make_singleton(session, record_a)
            _make_singleton(session, record_b)

            account_id = uuid.uuid4()
            session.add(
                Account(
                    account_id=account_id,
                    entity_id=entity_a,
                    product="personal_loan",
                    exposure=Decimal("1000.00"),
                    origination_date=date(2025, 1, 1),
                    current_state=DelinquencyBucket.CURRENT,
                )
            )
            session.flush()

        record_ids = tuple(sorted([record_a, record_b]))
        survivor_id = stable_cluster_id(list(record_ids))
        cluster = ResolvedCluster(entity_id=survivor_id, record_ids=record_ids, confidence=0.95)

        with session_scope() as session:
            n_merged = apply_resolution(session, [cluster], uuid.uuid4())
            assert n_merged == 2

        with session_scope() as session:
            account = session.get(Account, account_id)
            assert account.entity_id == survivor_id

            members = (
                session.execute(
                    select(EntityMember.entity_id).where(EntityMember.record_id.in_(record_ids))
                )
                .scalars()
                .all()
            )
            assert set(members) == {survivor_id}

            assert session.get(Entity, entity_a) is None
            assert session.get(Entity, survivor_id) is not None

    def test_rerunning_the_same_cluster_is_a_no_op(self) -> None:
        record_a, record_b = uuid.uuid4(), uuid.uuid4()
        with session_scope() as session:
            _make_party_record(session, record_a, "Marisol Quintrada")
            _make_party_record(session, record_b, "Mari Quintrada")
            _make_singleton(session, record_a)
            _make_singleton(session, record_b)

        record_ids = tuple(sorted([record_a, record_b]))
        survivor_id = stable_cluster_id(list(record_ids))
        cluster = ResolvedCluster(entity_id=survivor_id, record_ids=record_ids, confidence=0.95)

        with session_scope() as session:
            first_merged = apply_resolution(session, [cluster], uuid.uuid4())
        with session_scope() as session:
            second_merged = apply_resolution(session, [cluster], uuid.uuid4())

        assert first_merged == 2
        assert second_merged == 0

    def test_singleton_cluster_is_left_untouched(self) -> None:
        record_id = uuid.uuid4()
        with session_scope() as session:
            _make_party_record(session, record_id, "Youssef Ben Amara")
            original_entity_id = _make_singleton(session, record_id)

        cluster = ResolvedCluster(entity_id=uuid.uuid4(), record_ids=(record_id,), confidence=1.0)

        with session_scope() as session:
            n_merged = apply_resolution(session, [cluster], uuid.uuid4())
            assert n_merged == 0

        with session_scope() as session:
            member = session.get(EntityMember, (original_entity_id, record_id))
            assert member is not None
            assert session.get(Entity, original_entity_id) is not None
