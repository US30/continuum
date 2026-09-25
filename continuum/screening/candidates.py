"""Deterministic candidate generation: loose fuzzy match against the current sanctions
snapshot. CLAUDE.md §8: "Deterministic candidate generation (loose threshold, high
recall)." CLAUDE.md I5: this floor (Settings.screening_candidate_min_similarity) is looser
than the auto-reject floor on purpose — see continuum.config.settings.

For each entity, every one of its member party_records (an entity can have more than one
after continuum.resolution.merge) is compared against every current sanctions_alias; the
highest-similarity pairing is kept per (entity, listing). No LLM call anywhere in this
module (CLAUDE.md I1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.orm import Session

from continuum.config.settings import get_settings


class DobEvidence(StrEnum):
    MATCH = "match"  # equal, near-miss within the configured days, or day/month swapped
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"  # one side or both has no date of birth


def dob_evidence(entity_dob: date | None, listing_dob: date | None) -> DobEvidence:
    """Pure function. A missing date of birth is UNKNOWN, never MISMATCH: absence of
    evidence must not push a candidate toward rejection (I5)."""
    if entity_dob is None or listing_dob is None:
        return DobEvidence.UNKNOWN
    if abs((entity_dob - listing_dob).days) <= get_settings().screening_dob_near_miss_days:
        return DobEvidence.MATCH
    if entity_dob.year == listing_dob.year and (
        (entity_dob.month, entity_dob.day) == (listing_dob.day, listing_dob.month)
    ):
        return DobEvidence.MATCH  # day/month transposition
    return DobEvidence.MISMATCH


@dataclass(frozen=True)
class ScreeningCandidate:
    entity_id: uuid.UUID
    listing_id: str
    listing_primary_name: str
    matched_alias: str
    entity_name: str
    entity_date_of_birth: date | None
    listing_birth_date: date | None
    listing_countries: str | None
    listing_topics: str | None
    name_similarity: float

    @property
    def dob_match(self) -> bool:
        return self.dob_evidence is DobEvidence.MATCH

    @property
    def dob_evidence(self) -> DobEvidence:
        return dob_evidence(self.entity_date_of_birth, self.listing_birth_date)

    def as_evidence(self) -> dict[str, object]:
        return {
            "listing_id": self.listing_id,
            "listing_primary_name": self.listing_primary_name,
            "matched_alias": self.matched_alias,
            "entity_name": self.entity_name,
            "name_similarity": self.name_similarity,
            "dob_evidence": self.dob_evidence.value,
        }


_CANDIDATE_QUERY = text(
    """
    SELECT DISTINCT ON (em.entity_id, sa.listing_id)
        em.entity_id,
        sa.listing_id,
        se.primary_name AS listing_primary_name,
        sa.name AS matched_alias,
        pr.full_name AS entity_name,
        pr.date_of_birth AS entity_date_of_birth,
        se.birth_date AS listing_birth_date,
        se.countries AS listing_countries,
        se.topics AS listing_topics,
        similarity(pr.full_name, sa.name) AS name_similarity
    FROM entity_member em
    JOIN party_record pr ON pr.record_id = em.record_id
    JOIN sanctions_alias sa ON similarity(pr.full_name, sa.name) > :min_similarity
    JOIN sanctions_entity se ON se.listing_id = sa.listing_id AND se.is_current = true
    WHERE em.entity_id = ANY(:entity_ids)
    ORDER BY em.entity_id, sa.listing_id, similarity(pr.full_name, sa.name) DESC
    """
)


def generate_candidates(session: Session, entity_ids: list[uuid.UUID]) -> list[ScreeningCandidate]:
    if not entity_ids:
        return []

    settings = get_settings()
    rows = session.execute(
        _CANDIDATE_QUERY,
        {
            "min_similarity": settings.screening_candidate_min_similarity,
            "entity_ids": entity_ids,
        },
    ).all()

    return [
        ScreeningCandidate(
            entity_id=row.entity_id,
            listing_id=row.listing_id,
            listing_primary_name=row.listing_primary_name,
            matched_alias=row.matched_alias,
            entity_name=row.entity_name,
            entity_date_of_birth=row.entity_date_of_birth,
            listing_birth_date=row.listing_birth_date,
            listing_countries=row.listing_countries,
            listing_topics=row.listing_topics,
            name_similarity=float(row.name_similarity),
        )
        for row in rows
    ]


def current_snapshot_id(session: Session) -> uuid.UUID | None:
    """Every current sanctions_entity row shares one snapshot_id (continuum.foundation.
    sanctions replaces the whole table atomically) — any one row's value is authoritative.
    """
    row = session.execute(
        text("SELECT snapshot_id FROM sanctions_entity WHERE is_current = true LIMIT 1")
    ).first()
    return row[0] if row else None
