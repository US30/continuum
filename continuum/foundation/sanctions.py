"""OpenSanctions `targets.simple.csv` ingest, loaded via atomic snapshot swap.

CLAUDE.md §8: "Ingest OpenSanctions to snapshot tables with atomic swap." Format decision
(see plan): OpenSanctions' `targets.simple.csv` export, expected header:

    id,schema,name,aliases,birth_date,countries,topics

`aliases` is semicolon-joined and exploded here into one sanctions_alias row per alias.

Every run fully replaces the prior snapshot inside one transaction (delete + insert, not
UPDATE) — a reader either sees the old snapshot in full or the new one in full, never a
mix. Re-running on an unchanged input file is idempotent in content (CLAUDE.md I6): every
row's data is identical, even though snapshot_id itself is a fresh uuid4 each run by
design — a snapshot's whole purpose is to record *when* a given listing set was loaded.
"""

from __future__ import annotations

import csv
import hashlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

REQUIRED_COLUMNS = frozenset(
    {"id", "schema", "name", "aliases", "birth_date", "countries", "topics"}
)


@dataclass(frozen=True)
class SanctionsListingDraft:
    listing_id: str
    primary_name: str
    schema_type: str
    birth_date: date | None
    countries: str | None
    topics: str | None
    aliases: list[str]


def _parse_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_targets_simple_csv(path: Path) -> list[SanctionsListingDraft]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: targets.simple.csv missing columns: {sorted(missing)}")

        listings = []
        for row in reader:
            aliases = [a.strip() for a in row["aliases"].split(";") if a.strip()]
            listings.append(
                SanctionsListingDraft(
                    listing_id=row["id"],
                    primary_name=row["name"],
                    schema_type=row["schema"],
                    birth_date=_parse_date(row["birth_date"]),
                    countries=row["countries"] or None,
                    topics=row["topics"] or None,
                    aliases=aliases,
                )
            )
        return listings


SNAPSHOT_ID_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-0000000000a5")


def snapshot_id_for(path: Path) -> uuid.UUID:
    """Snapshot id derived from the listing file's bytes (CLAUDE.md I6: natural identifiers).

    Screening decisions are keyed on (entity, snapshot, listing). Reloading an unchanged file
    must therefore give the same snapshot id, or every existing decision would stop counting
    and every entity would fall back to "screening unresolved". A changed file gives a new
    id, which is exactly when earlier decisions should stop counting."""
    return uuid.uuid5(SNAPSHOT_ID_NAMESPACE, hashlib.sha256(path.read_bytes()).hexdigest())
