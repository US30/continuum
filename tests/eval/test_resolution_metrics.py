"""Tests for eval.resolution_metrics — precision/recall/F1 against ground_truth_link.
Requires a live Postgres.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from continuum.db.models.entity import Entity, EntityMember
from continuum.db.models.eval import CollisionType, GroundTruthLink
from continuum.db.models.party import PartyRecord
from continuum.db.session import session_scope
from eval.resolution_metrics import ResolutionMetrics, compute_resolution_metrics

pytestmark = pytest.mark.integration


def _record(session, name: str) -> uuid.UUID:
    record_id = uuid.uuid4()
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
    return record_id


def _entity(session) -> uuid.UUID:
    entity_id = uuid.uuid4()
    session.add(
        Entity(
            entity_id=entity_id,
            cluster_size=1,
            confidence=1.0,
            resolution_run_id=uuid.uuid4(),
        )
    )
    session.flush()
    return entity_id


def _member(session, entity_id: uuid.UUID, record_id: uuid.UUID) -> None:
    session.add(EntityMember(entity_id=entity_id, record_id=record_id, match_probability=1.0))
    session.flush()


class TestComputeResolutionMetrics:
    def test_correctly_merged_same_person_pair_is_a_true_positive(self) -> None:
        with session_scope() as session:
            record_a, record_b = _record(session, "A"), _record(session, "B")
            entity = _entity(session)
            _member(session, entity, record_a)
            _member(session, entity, record_b)
            session.add(
                GroundTruthLink(
                    link_id=uuid.uuid4(),
                    record_id_a=record_a,
                    record_id_b=record_b,
                    collision_type=CollisionType.SAME_PERSON,
                )
            )
            session.flush()

        with session_scope() as session:
            metrics = compute_resolution_metrics(session)
        assert metrics.true_positive >= 1

    def test_missed_same_person_pair_is_a_false_negative(self) -> None:
        with session_scope() as session:
            record_a, record_b = _record(session, "C"), _record(session, "D")
            _member(session, _entity(session), record_a)
            _member(session, _entity(session), record_b)
            session.add(
                GroundTruthLink(
                    link_id=uuid.uuid4(),
                    record_id_a=record_a,
                    record_id_b=record_b,
                    collision_type=CollisionType.SAME_PERSON,
                )
            )
            session.flush()

        with session_scope() as session:
            metrics = compute_resolution_metrics(session)
        assert metrics.false_negative >= 1

    def test_wrongly_merged_hard_negative_pair_is_a_false_positive(self) -> None:
        with session_scope() as session:
            record_a, record_b = _record(session, "E"), _record(session, "F")
            entity = _entity(session)
            _member(session, entity, record_a)
            _member(session, entity, record_b)
            session.add(
                GroundTruthLink(
                    link_id=uuid.uuid4(),
                    record_id_a=record_a,
                    record_id_b=record_b,
                    collision_type=CollisionType.HARD_NEGATIVE,
                )
            )
            session.flush()

        with session_scope() as session:
            metrics = compute_resolution_metrics(session)
        assert metrics.false_positive >= 1

    def test_correctly_separated_hard_negative_pair_is_a_true_negative(self) -> None:
        with session_scope() as session:
            record_a, record_b = _record(session, "G"), _record(session, "H")
            _member(session, _entity(session), record_a)
            _member(session, _entity(session), record_b)
            session.add(
                GroundTruthLink(
                    link_id=uuid.uuid4(),
                    record_id_a=record_a,
                    record_id_b=record_b,
                    collision_type=CollisionType.HARD_NEGATIVE,
                )
            )
            session.flush()

        with session_scope() as session:
            metrics = compute_resolution_metrics(session)
        assert metrics.true_negative >= 1

    def test_precision_recall_f1_formulas(self) -> None:
        metrics = ResolutionMetrics(
            true_positive=3, false_negative=1, false_positive=1, true_negative=5
        )
        assert metrics.precision == pytest.approx(0.75)
        assert metrics.recall == pytest.approx(0.75)
        assert metrics.f1 == pytest.approx(0.75)

    def test_no_positives_returns_none_rather_than_dividing_by_zero(self) -> None:
        metrics = ResolutionMetrics(
            true_positive=0, false_negative=0, false_positive=0, true_negative=0
        )
        assert metrics.precision is None
        assert metrics.recall is None
        assert metrics.f1 is None
