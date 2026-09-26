"""Synthetic identity attributes, and the messiness entity resolution must survive.

CLAUDE.md §8: "Attach synthetic identity attributes to party records" and plant collisions
with realistic noise — nicknames, transposed name order, transliteration-style drift,
typo'd addresses, formatting drift in phone/id. This module owns attribute generation and
that noise; continuum.foundation.collisions decides which records receive which variant
and records the ground truth (CLAUDE.md I4).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date

from faker import Faker

# Common nickname mappings, used to generate realistic first-name variants across
# duplicate records for the same person (CLAUDE.md §3: "hard-name-pair set" scope).
NICKNAMES: dict[str, tuple[str, ...]] = {
    "Robert": ("Rob", "Bob", "Bobby"),
    "William": ("Will", "Bill", "Billy"),
    "Elizabeth": ("Liz", "Beth", "Eliza"),
    "Michael": ("Mike", "Mickey"),
    "Katherine": ("Kate", "Katie", "Kathy"),
    "James": ("Jim", "Jimmy"),
    "Margaret": ("Maggie", "Meg", "Peggy"),
    "Richard": ("Rick", "Dick", "Rich"),
    "Jennifer": ("Jen", "Jenny"),
    "Christopher": ("Chris", "Topher"),
}

STREET_ABBREVIATIONS: dict[str, str] = {
    "Street": "St",
    "Avenue": "Ave",
    "Road": "Rd",
    "Drive": "Dr",
    "Lane": "Ln",
    "Boulevard": "Blvd",
}


@dataclass(frozen=True)
class SyntheticIdentity:
    full_name: str
    date_of_birth: date
    national_id: str | None
    address_line: str
    postal_code: str
    phone: str
    email: str


def generate_canonical_identity(fake: Faker, rng: random.Random) -> SyntheticIdentity:
    """The "clean" identity for one synthetic person, before any messiness is applied."""
    first, last = fake.first_name(), fake.last_name()
    return SyntheticIdentity(
        full_name=f"{first} {last}",
        date_of_birth=fake.date_of_birth(minimum_age=21, maximum_age=75),
        national_id=fake.unique.numerify("###-##-####"),
        address_line=fake.street_address(),
        postal_code=fake.postcode(),
        phone=fake.numerify("(###) ###-####"),
        email=fake.unique.email(),
    )


def name_variant(full_name: str, rng: random.Random) -> str:
    """One realistic name variant: nickname substitution, transposed order, or a small
    transliteration-style edit (doubled letter) — never all three at once.
    """
    parts = full_name.split(" ", 1)
    first, last = (parts[0], parts[1]) if len(parts) > 1 else (parts[0], "")

    choice = rng.random()
    if choice < 0.4 and first in NICKNAMES:
        first = rng.choice(NICKNAMES[first])
    elif choice < 0.6:
        return f"{last} {first}".strip()
    elif choice < 0.8 and len(last) > 3:
        idx = rng.randrange(1, len(last) - 1)
        last = last[:idx] + last[idx] + last[idx:]
    return f"{first} {last}".strip()


def address_variant(address: str, rng: random.Random) -> str:
    """Street-suffix abbreviation drift ("Street" -> "St"), the most common real-world
    address inconsistency between two records for the same person.
    """
    for full_form, abbrev in STREET_ABBREVIATIONS.items():
        if full_form in address:
            return address.replace(full_form, abbrev)
    return address


def phone_variant(phone: str, rng: random.Random) -> str:
    """Re-punctuate a phone number, e.g. "(555) 123-4567" -> "555-123-4567"."""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) != 10:
        return phone
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


def id_variant(national_id: str | None, rng: random.Random) -> str | None:
    """Strip formatting from a national id, e.g. "123-45-6789" -> "123456789"."""
    return None if national_id is None else national_id.replace("-", "")


def messy_duplicate(identity: SyntheticIdentity, rng: random.Random) -> SyntheticIdentity:
    """A duplicate record for the *same* real person. Messiness is applied to a random
    subset of fields, never all of them — real duplicate intake records usually still
    agree on most attributes (CLAUDE.md I5: resolution must stay recall-first against
    exactly this kind of partial disagreement).
    """
    return SyntheticIdentity(
        full_name=(
            name_variant(identity.full_name, rng) if rng.random() < 0.8 else identity.full_name
        ),
        date_of_birth=identity.date_of_birth,
        national_id=(
            id_variant(identity.national_id, rng) if rng.random() < 0.3 else identity.national_id
        ),
        address_line=(
            address_variant(identity.address_line, rng)
            if rng.random() < 0.5
            else identity.address_line
        ),
        postal_code=identity.postal_code,
        phone=phone_variant(identity.phone, rng) if rng.random() < 0.4 else identity.phone,
        email=identity.email,
    )


def reapplication_identity(identity: SyntheticIdentity, rng: random.Random) -> SyntheticIdentity:
    """The identity a person presents when they apply for another loan later: mostly the
    same details with the usual messiness, but the national id is often formatted differently
    or left off the form — exactly what defeats an exact-id match in an account-centric system
    while a probabilistic resolver still links it (continuum.foundation.build; Metric 3).
    """
    base = messy_duplicate(identity, rng)
    roll = rng.random()
    if roll < 0.40:
        national_id: str | None = None  # left blank
    elif roll < 0.80:
        national_id = id_variant(identity.national_id, rng)  # 123456789 vs 123-45-6789
    else:
        national_id = identity.national_id
    return SyntheticIdentity(
        full_name=base.full_name,
        date_of_birth=base.date_of_birth,
        national_id=national_id,
        address_line=base.address_line,
        postal_code=base.postal_code,
        phone=base.phone,
        email=base.email,
    )
