"""Phase 3 entrypoint: select -> candidates -> band -> (auto-decide | adjudicate) -> append.

Run via `make screen` (`python -m continuum.screening.run`). Every outcome — auto_reject,
auto_confirm, or adjudicated — is written to both screening_decision and risk_file_event
(CLAUDE.md §6, I3: the latter is append-only). Idempotent (CLAUDE.md I6): decision_id and
the corresponding risk_file_event's event_id are both derived from
(entity_id, snapshot_id, listing_id), so re-running against an unchanged sanctions snapshot
and unchanged entities produces no new rows — only a genuinely new snapshot or a newly
selected entity creates one.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

import typer
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from continuum.config.models import model_for
from continuum.config.settings import get_settings
from continuum.db.models.risk_file import RiskFileEvent, RiskFileEventType
from continuum.db.models.screening import ScreeningDecision, ScreeningMethod
from continuum.db.session import session_scope
from continuum.screening import candidates as candidates_mod
from continuum.screening.adjudicate import adjudicate, model_version
from continuum.screening.bands import ScreeningBand, band_for_score
from continuum.screening.candidates import ScreeningCandidate
from continuum.screening.select import select_entities_for_screening
from continuum.telemetry.logging import configure_logging, get_logger

app = typer.Typer(add_completion=False)
logger = get_logger(__name__)

DECISION_ID_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-0000000000d3")
EVENT_ID_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-0000000000e3")


def _decision_id(entity_id: uuid.UUID, snapshot_id: uuid.UUID, listing_id: str) -> uuid.UUID:
    return uuid.uuid5(DECISION_ID_NAMESPACE, f"{entity_id}:{snapshot_id}:{listing_id}")


def _write_decision_and_event(
    session: Session,
    *,
    entity_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    listing_id: str,
    method: ScreeningMethod,
    outcome: str,
    evidence: dict[str, object],
    confidence: float | None,
    model_version_str: str | None,
) -> None:
    decision_id = _decision_id(entity_id, snapshot_id, listing_id)
    stmt = pg_insert(ScreeningDecision).values(
        decision_id=decision_id,
        entity_id=entity_id,
        snapshot_id=snapshot_id,
        method=method,
        outcome=outcome,
        evidence=json.dumps(evidence),
        model_version=model_version_str,
        confidence=confidence,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["decision_id"],
        set_={
            "method": stmt.excluded.method,
            "outcome": stmt.excluded.outcome,
            "evidence": stmt.excluded.evidence,
            "model_version": stmt.excluded.model_version,
            "confidence": stmt.excluded.confidence,
        },
    )
    session.execute(stmt)

    event_id = uuid.uuid5(EVENT_ID_NAMESPACE, str(decision_id))
    event_stmt = pg_insert(RiskFileEvent).values(
        event_id=event_id,
        entity_id=entity_id,
        event_type=RiskFileEventType.SCREENING_OUTCOME,
        payload=json.dumps(
            {"decision_id": str(decision_id), "outcome": outcome, "method": method.value}
        ),
    )
    # Append-only (I3): a repeat of the same decision no-ops here rather than accumulating
    # duplicate history; a genuinely new decision always has a new decision_id and so
    # always inserts a new event.
    event_stmt = event_stmt.on_conflict_do_nothing(index_elements=["event_id"])
    session.execute(event_stmt)


def _decide_one(
    session: Session,
    candidate: ScreeningCandidate,
    snapshot_id: uuid.UUID,
    band: ScreeningBand,
) -> str:
    if band == ScreeningBand.AUTO_REJECT:
        _write_decision_and_event(
            session,
            entity_id=candidate.entity_id,
            snapshot_id=snapshot_id,
            listing_id=candidate.listing_id,
            method=ScreeningMethod.AUTO_REJECT,
            outcome="clear",
            evidence=candidate.as_evidence(),
            confidence=None,
            model_version_str=None,
        )
        return "auto_reject"

    if band == ScreeningBand.AUTO_CONFIRM:
        _write_decision_and_event(
            session,
            entity_id=candidate.entity_id,
            snapshot_id=snapshot_id,
            listing_id=candidate.listing_id,
            method=ScreeningMethod.AUTO_CONFIRM,
            outcome="hit",
            evidence=candidate.as_evidence(),
            confidence=None,
            model_version_str=None,
        )
        return "auto_confirm"

    verdict = adjudicate(candidate)
    model_id = model_for("screening_adjudicator")
    _write_decision_and_event(
        session,
        entity_id=candidate.entity_id,
        snapshot_id=snapshot_id,
        listing_id=candidate.listing_id,
        method=ScreeningMethod.ADJUDICATED,
        outcome="hit" if verdict.verdict == "confirm" else "clear",
        evidence={**candidate.as_evidence(), "reasoning": verdict.reasoning},
        confidence=verdict.confidence,
        model_version_str=model_version(model_id),
    )
    return f"adjudicated_{verdict.verdict}"


def run(hours_back: int = 24) -> None:
    settings = get_settings()
    since = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    outcome_counts: Counter[str] = Counter()

    with session_scope() as session:
        entity_ids = select_entities_for_screening(session, since)
        logger.info("screening.selected", n_entities=len(entity_ids))

        snapshot_id = candidates_mod.current_snapshot_id(session)
        if snapshot_id is None:
            typer.echo("no sanctions snapshot loaded — run `make data-build` first. Nothing to screen.")
            return

        all_candidates = candidates_mod.generate_candidates(session, entity_ids)
        reached_model = 0

        for candidate in all_candidates:
            band = band_for_score(candidate.name_similarity, settings)
            if band == ScreeningBand.ADJUDICATE:
                reached_model += 1
            outcome = _decide_one(session, candidate, snapshot_id, band)
            outcome_counts[outcome] += 1

    _print_summary(len(entity_ids), len(all_candidates), reached_model, outcome_counts)


def _print_summary(
    n_entities: int, total_candidates: int, reached_model: int, outcome_counts: Counter[str]
) -> None:
    typer.echo("")
    typer.echo("=== CONTINUUM screening summary ===")
    typer.echo(f"entities selected:      {n_entities}")
    typer.echo(f"candidates generated:   {total_candidates}")
    pct = (reached_model / total_candidates * 100) if total_candidates else 0.0
    typer.echo(f"reached adjudicator:    {reached_model} ({pct:.1f}% of candidates)")
    for outcome, count in sorted(outcome_counts.items()):
        typer.echo(f"  {outcome:<24} {count}")
    typer.echo("")


@app.command()
def main(
    hours_back: int = typer.Option(24, help="Look-back window for delinquency-state changes."),
) -> None:
    configure_logging(get_settings().log_level)
    logger.info("screening.start")
    run(hours_back)


if __name__ == "__main__":
    app()
