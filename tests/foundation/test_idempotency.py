"""Re-running the full Phase 1 build on the same seed must not duplicate or corrupt state
(CLAUDE.md I6). Requires a live Postgres — see docker/docker-compose.yml.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from continuum.db.models.account import Account, DelinquencyState, PaymentEvent
from continuum.db.models.entity import Entity
from continuum.db.models.eval import GroundTruthLink
from continuum.db.models.party import PartyRecord
from continuum.db.models.screening import SanctionsAlias, SanctionsEntity
from continuum.db.session import session_scope
from continuum.foundation.build import build

pytestmark = pytest.mark.integration

TABLES = (
    PartyRecord,
    Entity,
    Account,
    PaymentEvent,
    DelinquencyState,
    GroundTruthLink,
    SanctionsEntity,
    SanctionsAlias,
)


def _row_counts() -> dict[str, int]:
    with session_scope() as session:
        return {
            table.__tablename__: session.scalar(select(func.count()).select_from(table))
            for table in TABLES
        }


class TestBuildIdempotency:
    def test_running_build_twice_yields_identical_row_counts(self) -> None:
        build(n_persons=200, seed=987654321)
        first_counts = _row_counts()

        build(n_persons=200, seed=987654321)
        second_counts = _row_counts()

        assert first_counts == second_counts
        assert first_counts[PartyRecord.__tablename__] > 0


class TestBuildAfterResolution:
    def test_rebuilding_after_a_merge_does_not_resurrect_singletons_or_duplicate_members(
        self,
    ) -> None:
        import uuid

        from sqlalchemy import func, select

        from continuum.db.models.entity import EntityMember
        from continuum.resolution.cluster import ResolvedCluster, stable_cluster_id
        from continuum.resolution.merge import apply_resolution

        build(n_persons=120, seed=424242)
        with session_scope() as session:
            record_ids = list(session.scalars(select(EntityMember.record_id).limit(2)))
            cluster_ids = tuple(sorted(record_ids))
            apply_resolution(
                session,
                [ResolvedCluster(stable_cluster_id(list(cluster_ids)), cluster_ids, 0.99)],
                uuid.uuid4(),
            )
        before = _row_counts()
        with session_scope() as session:
            members_before = session.scalar(select(func.count()).select_from(EntityMember))

        build(n_persons=120, seed=424242)

        assert _row_counts() == before
        with session_scope() as session:
            assert session.scalar(select(func.count()).select_from(EntityMember)) == members_before
