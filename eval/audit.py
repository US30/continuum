"""Randomised audit stream — the nightly `audit.sample` of CLAUDE.md §7.

    audit.sample -> random slice of auto-cleared -> full pipeline (false-clear rate)

The screening pipeline auto-clears most entities without a model ever looking (auto-reject band
and "no candidate above the floor"). Its recall guarantee (CLAUDE.md I5) rests on a threshold
calibrated on 18 golden pairs, so it needs a continuous, *independent* check: draw a random
sample of entities the system cleared and re-screen them with a deliberately looser, slower
pipeline. Anything that pipeline flags that the production pipeline did not is a suspected
false clear.

The looser pipeline (no LLM): every listing alias against every member record's name with the
most generous similarity available — the max of trigram `similarity`, and `word_similarity` in
both directions (which tolerates padding and extra words) — at a much lower floor than
production, and DOB only ever used to *remove* a suspect when it clearly mismatches AND the name
score is weak. Optional `--adjudicate` would send suspects to the LLM (off by default; costs).

Two numbers are reported:
  * SAMPLE estimate, computable in production with no ground truth: the suspect rate among
    sampled auto-cleared entities, with a Wilson 95% interval.
  * CENSUS, using the eval-only planted-hit file (CLAUDE.md I4): of every planted true hit, how
    many did production actually clear? That is the exact false-clear rate on this dataset.
Run via `make audit` (`python -m eval.audit`).
"""

from __future__ import annotations

import csv
import json
import math
import random
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from continuum.config.settings import get_settings
from continuum.db.session import session_scope
from continuum.screening.candidates import DobEvidence, dob_evidence
from continuum.screening.status import ScreeningState, screening_state

PLANTED_PATH = Path("data") / "sanctions_planted.csv"

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


def load_planted(path: Path = PLANTED_PATH) -> dict[str, str]:
    """record_id -> listing_id for every planted true hit (eval-only truth)."""
    with path.open(newline="", encoding="utf-8") as fh:
        return {r["record_id"]: r["listing_id"] for r in csv.DictReader(fh)}


@dataclass(frozen=True)
class AuditReport:
    audit_date: str
    population: int
    sampled: int
    suspects: int
    suspect_rate: float
    suspect_rate_ci: tuple[float, float]
    suspects_that_are_true_hits: int
    planted_total: int
    planted_cleared_by_production: int
    planted_flagged_by_production: int
    false_clear_rate_census: float
    planted_cleared_but_audit_suspect: int


def run_audit(
    session: Session,
    audit_date: date,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = 0,
    planted: dict[str, str] | None = None,
) -> AuditReport:
    population = cleared_population(session)
    sampled = draw_sample([str(e) for e in population], sample_size * 2, audit_date, seed)
    # Keep only entities whose screening is genuinely CLEAR (no deferred candidate outstanding).
    confirmed = [
        uuid.UUID(e)
        for e in sampled
        if screening_state(session, uuid.UUID(e)) is ScreeningState.CLEAR
    ][:sample_size]

    hits = audit_rescreen(session, confirmed)
    suspect_entities = {h.entity_id for h in hits}

    planted = planted if planted is not None else load_planted()
    member_entity = {
        str(rec): str(ent)
        for rec, ent in session.execute(text("SELECT record_id, entity_id FROM entity_member"))
    }
    planted_entities = {
        member_entity[r]: listing for r, listing in planted.items() if r in member_entity
    }

    # Truth: which planted-hit entities did production leave clear / flag?
    states = {e: screening_state(session, uuid.UUID(e)) for e in planted_entities}
    cleared = [e for e, s in states.items() if s is ScreeningState.CLEAR]
    flagged = [e for e, s in states.items() if s is not ScreeningState.CLEAR]
    truly_sanctioned = set(planted_entities)
    cleared_and_suspect = {
        e
        for e in cleared
        if e in {h.entity_id for h in audit_rescreen(session, [uuid.UUID(c) for c in cleared])}
    }

    low, high = wilson_interval(len(suspect_entities), len(confirmed))
    return AuditReport(
        audit_date=audit_date.isoformat(),
        population=len(population),
        sampled=len(confirmed),
        suspects=len(suspect_entities),
        suspect_rate=len(suspect_entities) / max(len(confirmed), 1),
        suspect_rate_ci=(low, high),
        suspects_that_are_true_hits=len(suspect_entities & truly_sanctioned),
        planted_total=len(planted_entities),
        planted_cleared_by_production=len(cleared),
        planted_flagged_by_production=len(flagged),
        false_clear_rate_census=len(cleared) / max(len(planted_entities), 1),
        planted_cleared_but_audit_suspect=len(cleared_and_suspect),
    )


def report(as_json: bool = False, sample_size: int = DEFAULT_SAMPLE_SIZE) -> AuditReport:
    audit_date = get_settings().data_as_of_date
    with session_scope() as session:
        result = run_audit(session, audit_date, sample_size)
    if as_json:
        print(json.dumps(asdict(result)))
        return result

    lo, hi = result.suspect_rate_ci
    print("")
    print("=== CONTINUUM audit stream: random slice of auto-cleared entities, loose re-screen ===")
    print(
        f"audit date {result.audit_date}   auto-cleared population {result.population}"
        f"   sampled (confirmed clear) {result.sampled}"
    )
    print(
        f"suspects under the loose pipeline: {result.suspects}"
        f" = {100 * result.suspect_rate:.1f}% (95% CI {100 * lo:.1f}%..{100 * hi:.1f}%)"
    )
    print(f"  of which true planted hits: {result.suspects_that_are_true_hits}")
    print("")
    print("census against planted hits (eval-only truth):")
    print(f"  planted true hits: {result.planted_total}")
    print(
        f"  flagged by production: {result.planted_flagged_by_production}"
        f"   cleared by production (FALSE CLEARS): {result.planted_cleared_by_production}"
    )
    print(f"  false-clear rate (census): {100 * result.false_clear_rate_census:.1f}%")
    print(
        f"  false clears the audit's loose pipeline would have caught: "
        f"{result.planted_cleared_but_audit_suspect}/{result.planted_cleared_by_production}"
    )
    print("")
    return result


if __name__ == "__main__":
    report(as_json="--json" in sys.argv)
