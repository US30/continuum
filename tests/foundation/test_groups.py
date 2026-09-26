"""Fragmented identities, households and fraud rings — the structure Metric 3 measures."""

from __future__ import annotations

import collections

from continuum.foundation.collisions import build_party_records
from continuum.foundation.generate import RING_CURE_PROBABILITY, generate_population

SEED = 20260101
PERSONS = generate_population(3_000, SEED)
RECORDS, LINKS = build_party_records(PERSONS, SEED)
BY_PERSON = collections.defaultdict(list)
for record in RECORDS:
    BY_PERSON[record.person_id].append(record)


def test_every_account_is_opened_under_exactly_one_identity_record() -> None:
    seen = collections.Counter(a for r in RECORDS for a in r.account_ids)
    expected = {a.account_id for p in PERSONS for a in p.accounts}
    assert set(seen) == expected and set(seen.values()) == {1}


def test_some_people_open_later_accounts_under_a_fresh_identity() -> None:
    fragmented = [p for p in PERSONS if sum(1 for r in BY_PERSON[p.person_id] if r.account_ids) > 1]
    multi = [p for p in PERSONS if len(p.accounts) > 1]
    assert 0.2 < len(fragmented) / len(multi) < 0.7  # REAPPLY_RATE = 0.5, per extra account


def test_fragmented_identities_are_planted_as_same_person_links() -> None:
    linked = {frozenset((link.record_id_a, link.record_id_b)) for link in LINKS}
    for person in PERSONS:
        records = BY_PERSON[person.person_id]
        with_accounts = [r for r in records if r.account_ids]
        if len(with_accounts) > 1:
            canonical = records[0]
            assert any(
                frozenset((canonical.record_id, r.record_id)) in linked for r in with_accounts[1:]
            )


def test_reapplication_often_drifts_the_national_id() -> None:
    reapps = [r for r in RECORDS if "-app" in r.source_record_id]
    canonical = {r.person_id: r for r in RECORDS if r.source_record_id == str(r.person_id)}
    same = sum(
        r.identity.national_id == canonical[r.person_id].identity.national_id for r in reapps
    )
    assert reapps and 0.05 < same / len(reapps) < 0.45  # ~20% keep the exact id


def test_groups_exist_in_the_expected_proportions() -> None:
    kinds = collections.Counter(p.group_type for p in PERSONS)
    assert kinds["household"] > 0 and kinds["ring"] > 0
    assert 0.03 < kinds["household"] / len(PERSONS) < 0.12
    assert 0.01 < kinds["ring"] / len(PERSONS) < 0.06


def test_group_members_share_an_address_and_rings_share_a_phone() -> None:
    canonical = {r.person_id: r for r in RECORDS if r.source_record_id == str(r.person_id)}
    groups: dict[str, list] = collections.defaultdict(list)
    for p in PERSONS:
        if p.group_id:
            groups[p.group_id].append(p)
    for members in groups.values():
        addresses = {canonical[m.person_id].identity.address_line for m in members}
        assert len(addresses) == 1
        if members[0].group_type == "ring":
            assert len({canonical[m.person_id].identity.phone for m in members}) == 1


def test_ring_members_are_distinct_people_delinquent_together_and_rarely_cure() -> None:
    rings = [p for p in PERSONS if p.group_type == "ring"]
    assert len({p.person_id for p in rings}) == len(rings)
    episodes = [a.episode for p in rings for a in p.accounts if a.episode]
    assert len(episodes) > 0.8 * sum(len(p.accounts) for p in rings)
    cure_rate = sum(e.cured_60 for e in episodes) / len(episodes)
    assert cure_rate < RING_CURE_PROBABILITY + 0.12


def test_ring_accounts_go_delinquent_within_a_short_window() -> None:
    by_ring: dict[str, list] = collections.defaultdict(list)
    for p in PERSONS:
        if p.group_type == "ring":
            by_ring[p.group_id].extend(a.episode.decision_date for a in p.accounts if a.episode)
    for dates in by_ring.values():
        if len(dates) > 1:
            assert (max(dates) - min(dates)).days <= 25
