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
from continuum.db.session import session_scope
from continuum.foundation.build import build

pytestmark = pytest.mark.integration

TABLES = (PartyRecord, Entity, Account, PaymentEvent, DelinquencyState, GroundTruthLink)


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
