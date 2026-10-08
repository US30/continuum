"""The nightly `audit.sample` stage of CLAUDE.md §7 — the truth-free half of the audit stream.

    audit.sample -> random slice of auto-cleared -> full pipeline (false-clear rate)

The screening pipeline auto-clears most entities without a model ever looking (auto-reject band
and "no candidate above the floor"). This module draws a random sample of those entities and
re-screens them with a deliberately looser, slower pipeline (no LLM, I1): every listing alias
against every member record's name with the most generous similarity available — the max of
trigram `similarity`, and `word_similarity` in both directions — at a much lower floor than
production. Anything it flags that production did not is a *suspected* false clear.

Nothing here reads ground truth (I4). The census against planted hits lives in eval.audit,
which builds on these functions.
"""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from continuum.screening.candidates import DobEvidence, dob_evidence
from continuum.screening.status import ScreeningState, screening_state

DEFAULT_SAMPLE_SIZE = 400
# Audit-pipeline floor: far below production's screening_candidate_min_similarity (0.30), so
# it can surface what production's floor would have hidden. Chosen to make the audit a
# high-recall net; the suspect rate it produces is the audit's cost, reported not hidden.
AUDIT_FLOOR = 0.20
# A clearly mismatching date of birth removes a suspect only when the name evidence is weak.
DOB_EXCUSES_BELOW_SCORE = 0.60


@dataclass(frozen=True)
class AuditHit:
    entity_id: str
    listing_id: str
    score: float
    dob: str


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion; sane for 0 successes."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def is_suspect(score: float, dob: DobEvidence) -> bool:
    """The audit pipeline's flag: a generous name score, unless a *mismatching* DOB plus a weak
    name score rules it out. An unknown or matching DOB never excuses a suspect."""
    if score < AUDIT_FLOOR:
        return False
    return not (dob is DobEvidence.MISMATCH and score < DOB_EXCUSES_BELOW_SCORE)


def draw_sample(population: list[str], n: int, audit_date: date, seed: int = 0) -> list[str]:
    """Deterministic for (date, seed): the audit is random across nights but a rerun of the
    same night audits the same entities (CLAUDE.md I6)."""
    rng = random.Random(f"audit:{audit_date.isoformat()}:{seed}")
    ordered = sorted(population)
    return sorted(rng.sample(ordered, min(n, len(ordered))))


_AUDIT_QUERY = text(
    """
    SELECT em.entity_id, sa.listing_id, pr.date_of_birth, se.birth_date,
           GREATEST(
               similarity(pr.full_name, sa.name),
               word_similarity(sa.name, pr.full_name),
               word_similarity(pr.full_name, sa.name)
           ) AS score
    FROM entity_member em
    JOIN party_record pr ON pr.record_id = em.record_id
    JOIN sanctions_alias sa ON GREATEST(
               similarity(pr.full_name, sa.name),
               word_similarity(sa.name, pr.full_name),
               word_similarity(pr.full_name, sa.name)) >= :floor
    JOIN sanctions_entity se ON se.listing_id = sa.listing_id AND se.is_current = true
    WHERE em.entity_id = ANY(:entity_ids)
    """
)


def audit_rescreen(session: Session, entity_ids: list[uuid.UUID]) -> list[AuditHit]:
    """Best suspect (entity, listing) pairs under the loose pipeline; one row per pair."""
    if not entity_ids:
        return []
    best: dict[tuple[str, str], AuditHit] = {}
    rows = session.execute(_AUDIT_QUERY, {"floor": AUDIT_FLOOR, "entity_ids": entity_ids})
    for entity_id, listing_id, customer_dob, listing_dob, score in rows:
        evidence = dob_evidence(customer_dob, listing_dob)
        if not is_suspect(float(score), evidence):
            continue
        key = (str(entity_id), str(listing_id))
        if key not in best or float(score) > best[key].score:
            best[key] = AuditHit(str(entity_id), str(listing_id), float(score), evidence.value)
    return sorted(best.values(), key=lambda h: (-h.score, h.entity_id))


def cleared_population(session: Session) -> list[uuid.UUID]:
    """Entities production auto-cleared: at least one auto-reject/clear decision on the current
    snapshot and no hit. (Fully-resolved status is confirmed per sampled entity below.)"""
    rows = session.execute(
        text(
            """
            SELECT DISTINCT d.entity_id
            FROM screening_decision d
            WHERE d.outcome = 'clear' AND d.method = 'AUTO_REJECT'
              AND d.entity_id NOT IN (
                  SELECT entity_id FROM screening_decision WHERE outcome = 'hit'
              )
            """
        )
    ).all()
    return [r[0] for r in rows]


@dataclass(frozen=True)
class SampleAudit:
    population: int
    sampled: list[uuid.UUID]
    hits: list[AuditHit]

    @property
    def suspects(self) -> set[str]:
        return {h.entity_id for h in self.hits}

    @property
    def suspect_rate(self) -> float:
        return len(self.suspects) / max(len(self.sampled), 1)

    @property
    def suspect_rate_ci(self) -> tuple[float, float]:
        return wilson_interval(len(self.suspects), len(self.sampled))


def sample_audit(
    session: Session, audit_date: date, sample_size: int = DEFAULT_SAMPLE_SIZE, seed: int = 0
) -> SampleAudit:
    """Sample auto-cleared entities for `audit_date` and re-screen them with the loose pipeline."""
    population = cleared_population(session)
    drawn = draw_sample([str(e) for e in population], sample_size * 2, audit_date, seed)
    # Keep only entities whose screening is genuinely CLEAR (no deferred candidate outstanding).
    confirmed = [
        uuid.UUID(e)
        for e in drawn
        if screening_state(session, uuid.UUID(e)) is ScreeningState.CLEAR
    ][:sample_size]
    return SampleAudit(len(population), confirmed, audit_rescreen(session, confirmed))
