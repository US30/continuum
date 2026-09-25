"""Tests for planted identity collisions.

CLAUDE.md I4: ground_truth_link is eval-only, and continuum.foundation.collisions is its
only writer. These tests check the planting logic itself (pure, no DB); the import-boundary
side of I4 is enforced separately in tests/test_invariants.py.
"""

from __future__ import annotations

import uuid

from continuum.db.models.eval import CollisionType
from continuum.foundation.collisions import build_party_records
from continuum.foundation.generate import SyntheticPerson

SEED = 20260101


def _bare_persons(n: int) -> list[SyntheticPerson]:
    return [SyntheticPerson(person_id=uuid.UUID(int=i + 1), accounts=[]) for i in range(n)]


class TestPlantedLinks:
    def test_every_same_person_link_points_at_records_of_one_person(self) -> None:
        persons = _bare_persons(200)
        records, links = build_party_records(persons, SEED)

        person_by_record = {r.record_id: r.person_id for r in records}
        for link in links:
            if link.collision_type == CollisionType.SAME_PERSON:
                assert person_by_record[link.record_id_a] == person_by_record[link.record_id_b]

    def test_hard_negatives_link_different_persons(self) -> None:
        persons = _bare_persons(500)
        records, links = build_party_records(persons, SEED)
        person_by_record = {r.record_id: r.person_id for r in records}

        hard_negatives = [
            link for link in links if link.collision_type == CollisionType.HARD_NEGATIVE
        ]
        assert hard_negatives, "expected at least one hard negative at this population size"
        for link in hard_negatives:
            assert person_by_record[link.record_id_a] != person_by_record[link.record_id_b]

    def test_every_record_id_in_a_link_exists_in_records(self) -> None:
        persons = _bare_persons(300)
        records, links = build_party_records(persons, SEED)
        record_ids = {r.record_id for r in records}
        for link in links:
            assert link.record_id_a in record_ids
            assert link.record_id_b in record_ids

    def test_deterministic_given_same_seed(self) -> None:
        persons = _bare_persons(100)
        records_a, links_a = build_party_records(persons, SEED)
        records_b, links_b = build_party_records(persons, SEED)
        assert records_a == records_b
        assert links_a == links_b

    def test_hard_negatives_share_full_name_but_not_person(self) -> None:
        persons = _bare_persons(500)
        records, links = build_party_records(persons, SEED)
        records_by_id = {r.record_id: r for r in records}

        for link in links:
            if link.collision_type == CollisionType.HARD_NEGATIVE:
                record_a = records_by_id[link.record_id_a]
                record_b = records_by_id[link.record_id_b]
                assert record_a.identity.full_name == record_b.identity.full_name
                assert record_a.person_id != record_b.person_id
