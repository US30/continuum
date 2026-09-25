"""Synthetic sanctions/PEP listing in OpenSanctions `targets.simple.csv` format.

CLAUDE.md I8: no real personal data in any environment — so the local default listing is
generated, not downloaded. A real OpenSanctions export dropped at data/targets.simple.csv
is still loaded by continuum.foundation.sanctions unchanged; this module only fills the
file when it is absent.

Three kinds of listing, indistinguishable by listing_id (ids are assigned after shuffling):
  - planted:  a name variant of a real synthetic customer, with an exact, day-shifted or
              missing date of birth — the true hits screening must never miss (I5).
  - hard decoy: the exact name of a synthetic customer but a different date of birth — the
              "common name" false positive the adjudicator exists for.
  - decoy:    an unrelated random name.

The planted customer -> listing mapping is written to a *separate* file
(data/sanctions_planted.csv) for the eval harness only, mirroring how ground_truth_link
stays out of runtime code (CLAUDE.md I4).
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from faker import Faker

from continuum.foundation.collisions import PartyRecordDraft
from continuum.foundation.identity import name_variant

HEADER = ["id", "schema", "name", "aliases", "birth_date", "countries", "topics"]

PLANTED_FRACTION = 0.01  # of canonical records; at least MIN_PLANTED
MIN_PLANTED = 3
HARD_DECOYS_PER_PLANTED = 2
DECOYS_PER_PLANTED = 8
TOPICS = ("sanction", "pep")
COUNTRY = "zz"  # ISO 3166 user-assigned code: unmistakably not a real country


@dataclass(frozen=True)
class _Row:
    name: str
    aliases: list[str]
    birth_date: date | None
    topics: str
    planted_record_id: str | None = None


def build_rows(drafts: list[PartyRecordDraft], seed: int) -> list[_Row]:
    rng = random.Random(seed)
    fake = Faker()
    fake.seed_instance(seed + 1)

    canonical = sorted(
        (d for d in drafts if d.source_system == "loan_origination"), key=lambda d: d.record_id
    )
    n_planted = min(len(canonical), max(MIN_PLANTED, int(len(canonical) * PLANTED_FRACTION)))
    planted = rng.sample(canonical, n_planted)

    rows: list[_Row] = []
    for draft in planted:
        name = draft.identity.full_name
        roll = rng.random()
        if roll < 0.7:
            dob: date | None = draft.identity.date_of_birth
        elif roll < 0.9:
            dob = draft.identity.date_of_birth + timedelta(days=rng.choice([-1, 1, 30, -30]))
        else:
            dob = None
        rows.append(
            _Row(
                name=name_variant(name, rng),
                aliases=sorted({name, name_variant(name, rng)}),
                birth_date=dob,
                topics=rng.choice(TOPICS),
                planted_record_id=str(draft.record_id),
            )
        )

    for draft in rng.sample(canonical, min(len(canonical), n_planted * HARD_DECOYS_PER_PLANTED)):
        rows.append(
            _Row(
                name=draft.identity.full_name,
                aliases=[],
                birth_date=fake.date_of_birth(minimum_age=21, maximum_age=80),
                topics=rng.choice(TOPICS),
            )
        )

    for _ in range(n_planted * DECOYS_PER_PLANTED):
        rows.append(
            _Row(
                name=f"{fake.first_name()} {fake.last_name()}",
                aliases=[],
                birth_date=fake.date_of_birth(minimum_age=21, maximum_age=80),
                topics=rng.choice(TOPICS),
            )
        )

    rng.shuffle(rows)
    return rows


def write_synthetic_sanctions(
    drafts: list[PartyRecordDraft], seed: int, csv_path: Path, planted_path: Path
) -> int:
    """Writes the listing CSV and the eval-only planted mapping. Returns listing count.
    Deterministic given (drafts, seed), so re-running rewrites identical files (I6).
    """
    rows = build_rows(drafts, seed)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    planted_pairs: list[tuple[str, str]] = []
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        for i, row in enumerate(rows, start=1):
            listing_id = f"synth-{i:05d}"
            writer.writerow(
                [
                    listing_id,
                    "Person",
                    row.name,
                    ";".join(row.aliases),
                    row.birth_date.isoformat() if row.birth_date else "",
                    COUNTRY,
                    row.topics,
                ]
            )
            if row.planted_record_id:
                planted_pairs.append((row.planted_record_id, listing_id))

    with planted_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["record_id", "listing_id"])
        writer.writerows(sorted(planted_pairs))

    return len(rows)
