"""Tests for continuum.screening.select. Requires a live Postgres."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from continuum.db.models.account import Account, DelinquencyBucket, DelinquencyState
from continuum.db.models.entity import Entity
from continuum.db.models.screening import ScreeningDecision, ScreeningMethod
from continuum.db.session import session_scope
from continuum.screening.select import select_entities_for_screening

pytestmark = pytest.mark.integration

SINGLETON_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _entity(session) -> uuid.UUID:
    entity_id = uuid.uuid4()
    session.add(
        Entity(
            entity_id=entity_id,
            cluster_size=1,
            confidence=Decimal("1.0"),
            resolution_run_id=SINGLETON_RUN_ID,
        )
    )
    session.flush()
    return entity_id


class TestSelectEntitiesForScreening:
    def test_never_screened_entity_is_selected(self) -> None:
        with session_scope() as session:
            entity_id = _entity(session)

        since = datetime.now(UTC) - timedelta(days=1)
        with session_scope() as session:
            selected = select_entities_for_screening(session, since)
        assert entity_id in selected

    def test_already_screened_entity_with_no_recent_change_is_not_reselected(self) -> None:
        with session_scope() as session:
            entity_id = _entity(session)
            session.add(
                ScreeningDecision(
                    decision_id=uuid.uuid4(),
                    entity_id=entity_id,
                    snapshot_id=uuid.uuid4(),
                    method=ScreeningMethod.AUTO_REJECT,
                    outcome="clear",
                )
            )
            session.flush()

        since = datetime.now(UTC) - timedelta(days=1)
        with session_scope() as session:
            selected = select_entities_for_screening(session, since)
        assert entity_id not in selected

    def test_delinquency_state_change_since_cutoff_reselects_a_screened_entity(self) -> None:
        with session_scope() as session:
            entity_id = _entity(session)
            session.add(
                ScreeningDecision(
                    decision_id=uuid.uuid4(),
                    entity_id=entity_id,
                    snapshot_id=uuid.uuid4(),
                    method=ScreeningMethod.AUTO_REJECT,
                    outcome="clear",
                )
            )
            session.flush()
            account_id = uuid.uuid4()
            session.add(
                Account(
                    account_id=account_id,
                    entity_id=entity_id,
                    product="personal_loan",
                    exposure=Decimal("500.00"),
                    origination_date=date(2025, 1, 1),
                    current_state=DelinquencyBucket.DPD_30,
                )
            )
            session.flush()
            now = datetime.now(UTC)
            session.add(
                DelinquencyState(
                    account_id=account_id,
                    effective_from=now,
                    effective_to=None,
                    state=DelinquencyBucket.DPD_30,
                    days_past_due=30,
                )
            )
            session.flush()

        since = datetime.now(UTC) - timedelta(hours=1)
        with session_scope() as session:
            selected = select_entities_for_screening(session, since)
        assert entity_id in selected

    def test_delinquency_state_change_before_cutoff_does_not_reselect(self) -> None:
        with session_scope() as session:
            entity_id = _entity(session)
            session.add(
                ScreeningDecision(
                    decision_id=uuid.uuid4(),
                    entity_id=entity_id,
                    snapshot_id=uuid.uuid4(),
                    method=ScreeningMethod.AUTO_REJECT,
                    outcome="clear",
                )
            )
            session.flush()
            account_id = uuid.uuid4()
            session.add(
                Account(
                    account_id=account_id,
                    entity_id=entity_id,
                    product="personal_loan",
                    exposure=Decimal("500.00"),
                    origination_date=date(2020, 1, 1),
                    current_state=DelinquencyBucket.CURED,
                )
            )
            session.flush()
            old = datetime.now(UTC) - timedelta(days=100)
            session.add(
                DelinquencyState(
                    account_id=account_id,
                    effective_from=old,
                    effective_to=None,
                    state=DelinquencyBucket.CURED,
                    days_past_due=0,
                )
            )
            session.flush()

        since = datetime.now(UTC) - timedelta(hours=1)
        with session_scope() as session:
            selected = select_entities_for_screening(session, since)
        assert entity_id not in selected
