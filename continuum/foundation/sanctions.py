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


def new_snapshot_id() -> uuid.UUID:
    return uuid.uuid4()
