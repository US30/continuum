"""Audit stream: pure statistics/sampling, plus the loose re-screen against the real SQL."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete

from continuum.db.models.entity import Entity, EntityMember
from continuum.db.models.party import PartyRecord
from continuum.db.models.screening import SanctionsAlias, SanctionsEntity
from continuum.db.session import session_scope
from continuum.screening.audit import (
    AUDIT_FLOOR,
    audit_rescreen,
    draw_sample,
    is_suspect,
    wilson_interval,
)
from continuum.screening.candidates import DobEvidence


class TestWilson:
    def test_zero_successes_has_an_upper_bound_above_zero(self) -> None:
        low, high = wilson_interval(0, 100)
        assert low == 0.0 and 0.03 < high < 0.05  # the "rule of three" ballpark

    def test_known_value(self) -> None:
        low, high = wilson_interval(50, 100)
        assert low == pytest.approx(0.404, abs=0.005) and high == pytest.approx(0.596, abs=0.005)

    def test_empty_sample_is_uninformative(self) -> None:
        assert wilson_interval(0, 0) == (0.0, 1.0)

    def test_interval_contains_the_point_estimate_and_narrows_with_n(self) -> None:
        a, b = wilson_interval(10, 100), wilson_interval(100, 1000)
        assert a[0] <= 0.1 <= a[1] and (b[1] - b[0]) < (a[1] - a[0])


class TestSuspect:
    def test_below_the_audit_floor_is_never_suspect(self) -> None:
        assert not is_suspect(AUDIT_FLOOR - 0.01, DobEvidence.UNKNOWN)

    def test_generous_score_with_unknown_or_matching_dob_is_suspect(self) -> None:
        assert is_suspect(0.3, DobEvidence.UNKNOWN) and is_suspect(0.3, DobEvidence.MATCH)

    def test_a_mismatching_dob_excuses_only_a_weak_name_match(self) -> None:
        assert not is_suspect(0.3, DobEvidence.MISMATCH)
        assert is_suspect(0.9, DobEvidence.MISMATCH)  # near-identical name: a DOB typo is possible


class TestSampling:
    def test_same_night_same_sample_different_night_different_sample(self) -> None:
        pop = [f"e{i}" for i in range(500)]
        night = date(2026, 1, 1)
        assert draw_sample(pop, 50, night) == draw_sample(list(reversed(pop)), 50, night)
        assert draw_sample(pop, 50, night) != draw_sample(pop, 50, date(2026, 1, 2))

    def test_sample_is_bounded_by_the_population(self) -> None:
        assert len(draw_sample(["a", "b"], 10, date(2026, 1, 1))) == 2


@pytest.mark.integration
class TestLooseRescreen:
    @pytest.fixture(autouse=True)
    def _remove_listings_afterwards(self) -> Iterator[None]:
        """The test database holds exactly one sanctions snapshot (graph tests rely on it), so
        every listing these tests add must be removed again."""
        self.created: list[str] = []
        yield
        with session_scope() as s:
            s.execute(delete(SanctionsAlias).where(SanctionsAlias.listing_id.in_(self.created)))
            s.execute(delete(SanctionsEntity).where(SanctionsEntity.listing_id.in_(self.created)))

    def _entity(self, name: str, dob: date | None = None) -> uuid.UUID:
        entity_id, record_id = uuid.uuid4(), uuid.uuid4()
        with session_scope() as s:
            s.add(
                Entity(
                    entity_id=entity_id,
                    cluster_size=1,
                    confidence=Decimal("1"),
                    resolution_run_id=uuid.uuid4(),
                )
            )
            s.add(
                PartyRecord(
                    record_id=record_id,
                    source_system="t",
                    source_record_id=str(record_id),
                    full_name=name,
                    date_of_birth=dob,
                )
            )
            s.flush()
            s.add(
                EntityMember(
                    entity_id=entity_id, record_id=record_id, match_probability=Decimal("1")
                )
            )
        return entity_id

    def _listing(self, name: str, dob: date | None = None) -> str:
        listing_id = f"audit-{uuid.uuid4().hex[:8]}"
        self.created.append(listing_id)
        with session_scope() as s:
            s.add(
                SanctionsEntity(
                    listing_id=listing_id,
                    snapshot_id=uuid.uuid4(),
                    primary_name=name,
                    schema_type="Person",
                    birth_date=dob,
                    is_current=True,
                )
            )
            s.flush()
            s.add(SanctionsAlias(alias_id=uuid.uuid4(), listing_id=listing_id, name=name))
        return listing_id

    def test_a_padded_name_that_production_similarity_would_miss_is_caught(self) -> None:
        listing = self._listing("Zephyrine Quillfeather")
        padded = self._entity("Zephyrine Quillfeather Xanthippe Wolfgang Roundtree Bellwether")
        with session_scope() as s:
            hits = audit_rescreen(s, [padded])
        assert any(h.listing_id == listing for h in hits)

    def test_an_unrelated_name_is_not_flagged(self) -> None:
        self._listing("Zephyrine Quillfeather")
        other = self._entity("Bartholomew Nightingale")
        with session_scope() as s:
            assert audit_rescreen(s, [other]) == []

    def test_no_entities_means_no_work(self) -> None:
        with session_scope() as s:
            assert audit_rescreen(s, []) == []
