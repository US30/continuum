"""Plants identity collisions and records them in ground_truth_link.

CLAUDE.md §8: "Plant identity collisions and record them in ground_truth_link." CLAUDE.md
I4: this is the only module anywhere in the codebase that writes ground_truth_link.

Two kinds of planted pairs:
  - SAME_PERSON: two or more party_records that are messy duplicates of one real person
    (CLAUDE.md §2 scenario — one individual behind several accounts).
  - HARD_NEGATIVE: two party_records for genuinely *different* people, deliberately made
    to look similar (shared name, near-miss DOB), so screening/resolution precision
    (CLAUDE.md §3, the hard-name-pair set) means something rather than being measured
    against only easy cases.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from itertools import pairwise

from faker import Faker

from continuum.db.models.eval import CollisionType
from continuum.foundation.generate import SyntheticPerson
from continuum.foundation.identity import (
    SyntheticIdentity,
    generate_canonical_identity,
    messy_duplicate,
    reapplication_identity,
)

# Fraction of persons who get 2-4 duplicate party_records (SAME_PERSON collisions), and
# fraction of the canonical-record population paired off as HARD_NEGATIVEs. These describe
# the *shape of the synthetic dataset*, not a production business rule, so they live here
# rather than in Settings (CLAUDE.md §10's "no magic thresholds" targets runtime logic).
MULTI_RECORD_RATE = 0.12
HARD_NEGATIVE_RATE = 0.03

# Probability that each additional account a person opens is applied for under a *new*
# identity record (a re-application with slightly different details) rather than the
# original one. This is what makes one person look like several customers to a system that
# links accounts by exact identifiers (Phase 7, Metric 3).
REAPPLY_RATE = 0.5
# In a household, the chance a member shares the household landline as well as the address.
HOUSEHOLD_SHARED_PHONE_RATE = 0.3


@dataclass(frozen=True)
class PartyRecordDraft:
    record_id: uuid.UUID
    person_id: uuid.UUID
    source_system: str
    source_record_id: str
    identity: SyntheticIdentity
    #: Accounts opened under this identity record (empty for pure duplicates/intake records).
    account_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class PlantedLink:
    record_id_a: uuid.UUID
    record_id_b: uuid.UUID
    collision_type: CollisionType


def build_party_records(
    persons: list[SyntheticPerson], seed: int
) -> tuple[list[PartyRecordDraft], list[PlantedLink]]:
    """Deterministic given (persons, seed) — persons must itself come from
    generate.generate_population(n, seed) with the same seed for CLAUDE.md I6 idempotency.
    """
    rng = random.Random(seed)
    fake = Faker()
    fake.seed_instance(seed)

    records: list[PartyRecordDraft] = []
    links: list[PlantedLink] = []

    shared_by_group: dict[str, SyntheticIdentity] = {}

    for person in persons:
        canonical_identity = _with_group_contact(
            generate_canonical_identity(fake, rng), person, shared_by_group, rng
        )
        canonical_record_id = uuid.UUID(int=rng.getrandbits(128))
        canonical_accounts = [person.accounts[0].account_id] if person.accounts else []
        person_record_ids = [canonical_record_id]
        extra_records: list[PartyRecordDraft] = []

        # Later accounts: sometimes opened under a fresh, messier identity record.
        for k, account in enumerate(person.accounts[1:], start=1):
            if rng.random() < REAPPLY_RATE:
                app_id = uuid.UUID(int=rng.getrandbits(128))
                extra_records.append(
                    PartyRecordDraft(
                        record_id=app_id,
                        person_id=person.person_id,
                        source_system="loan_origination",
                        source_record_id=f"{person.person_id}-app{k}",
                        identity=reapplication_identity(canonical_identity, rng),
                        account_ids=(account.account_id,),
                    )
                )
                person_record_ids.append(app_id)
            else:
                canonical_accounts.append(account.account_id)

        records.append(
            PartyRecordDraft(
                record_id=canonical_record_id,
                person_id=person.person_id,
                source_system="loan_origination",
                source_record_id=str(person.person_id),
                identity=canonical_identity,
                account_ids=tuple(canonical_accounts),
            )
        )
        records.extend(extra_records)

        n_duplicates = 0
        if rng.random() < MULTI_RECORD_RATE:
            n_duplicates = rng.choices([1, 2, 3], weights=[0.6, 0.3, 0.1])[0]

        for i in range(n_duplicates):
            dup_id = uuid.UUID(int=rng.getrandbits(128))
            records.append(
                PartyRecordDraft(
                    record_id=dup_id,
                    person_id=person.person_id,
                    source_system="collections_intake",
                    source_record_id=f"{person.person_id}-dup{i}",
                    identity=messy_duplicate(canonical_identity, rng),
                )
            )
            person_record_ids.append(dup_id)

        for record_a, record_b in pairwise(person_record_ids):
            links.append(PlantedLink(record_a, record_b, CollisionType.SAME_PERSON))

    _plant_hard_negatives(records, links, rng)
    return records, links


def _with_group_contact(
    identity: SyntheticIdentity,
    person: SyntheticPerson,
    shared_by_group: dict[str, SyntheticIdentity],
    rng: random.Random,
) -> SyntheticIdentity:
    """Give household/ring members the group's shared contact points. The first member of a
    group defines them; later members inherit the address (both group types) and, for rings,
    the phone too (households share a landline only sometimes)."""
    if person.group_id is None:
        return identity
    anchor = shared_by_group.setdefault(person.group_id, identity)
    if anchor is identity:
        return identity
    share_phone = person.group_type == "ring" or rng.random() < HOUSEHOLD_SHARED_PHONE_RATE
    return SyntheticIdentity(
        full_name=identity.full_name,
        date_of_birth=identity.date_of_birth,
        national_id=identity.national_id,
        address_line=anchor.address_line,
        postal_code=anchor.postal_code,
        phone=anchor.phone if share_phone else identity.phone,
        email=identity.email,
    )


def _plant_hard_negatives(
    records: list[PartyRecordDraft], links: list[PlantedLink], rng: random.Random
) -> None:
    """Add HARD_NEGATIVE pairs in place: pick two distinct persons' canonical records and
    give one an extra record that shares the other's exact name, while every other
    attribute (DOB, address, phone, national id, email) still belongs to its own person.
    Not linked as SAME_PERSON — resolution must learn to tell these apart.
    """
    canonical_records = [r for r in records if r.source_system == "loan_origination"]
    n_hard_negatives = int(len(canonical_records) * HARD_NEGATIVE_RATE)

    for i in range(n_hard_negatives):
        if len(canonical_records) < 2:
            break
        record_a, record_b = rng.sample(canonical_records, 2)
        nudged_identity = SyntheticIdentity(
            full_name=record_a.identity.full_name,
            date_of_birth=record_b.identity.date_of_birth,
            national_id=record_b.identity.national_id,
            address_line=record_b.identity.address_line,
            postal_code=record_b.identity.postal_code,
            phone=record_b.identity.phone,
            email=record_b.identity.email,
        )
        nudged_record_id = uuid.UUID(int=rng.getrandbits(128))
        records.append(
            PartyRecordDraft(
                record_id=nudged_record_id,
                person_id=record_b.person_id,
                source_system="collections_intake",
                source_record_id=f"{record_b.person_id}-hardneg{i}",
                identity=nudged_identity,
            )
        )
        links.append(PlantedLink(record_a.record_id, nudged_record_id, CollisionType.HARD_NEGATIVE))
