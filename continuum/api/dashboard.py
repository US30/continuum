"""Everything the dashboard page shows, read in one pass. Counts and one short case list only.

Two sources:

  - the database: what the nightly batch and the decisioning graph wrote (aggregates, plus the
    top-ranked cases by entity id — no name, address or other personal field is read);
  - the metrics report: a JSON file the evaluation harness writes (`make metrics`). It is read
    as a file. This module does not import the harness and does not read ground truth (I4).
    When the file is absent the three metrics are shown as not computed, never guessed.

No LLM, no writes.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from continuum.db.models.account import Account, DelinquencyBucket
from continuum.db.models.collections import Case, ContactEvent, ContactSource
from continuum.db.models.entity import Entity, EntityEdge
from continuum.db.models.party import PartyRecord
from continuum.db.models.risk_file import RiskFileEvent
from continuum.db.models.screening import SanctionsAlias, SanctionsEntity, ScreeningDecision
from continuum.graph.nodes.case_assembly import DELINQUENT
from continuum.screening.candidates import current_snapshot_id

# How many of the top-ranked cases the page lists. A page length, not a model threshold.
TOP_CASES = 15

# The report's blocks, in CLAUDE.md §9 order, with the title each is shown under.
METRIC_BLOCKS = (
    ("metric1_screening", "Screening"),
    ("metric2_uplift", "Uplift ranking"),
    ("metric3_network", "Entity view vs. account view"),
    ("supporting", "Supporting checks"),
)

# The one value each metric block leads with: report key, how it is written, and the caption
# under it. A block with no entry, or whose report lacks the key, shows its table only. The
# report has no precision figure for Metric 1 until the adjudicator has been run on the
# labelled set, so that block leads with the recall the deterministic layer must hold (I5).
HEADLINES = {
    "metric1_screening": (
        "recall_deterministic_layer",
        "percent",
        "Metric 1 · recall of the deterministic layer",
    ),
    "metric2_uplift": (
        "vs_rules_top_k",
        "currency",
        "Metric 2 · incremental recovery vs. the rules baseline",
    ),
    "metric3_network": (
        "validated_share",
        "percent",
        "Metric 3 · cases where the entity view found what the account view missed",
    ),
}


def _written(value: float, style: str) -> str:
    if style == "percent":
        return f"{value:.1%}"
    return f"${value:,.0f}"


class Count(BaseModel):
    label: str
    n: int


class StateRow(BaseModel):
    state: str
    accounts: int
    exposure: float


class ScreeningRow(BaseModel):
    method: str
    outcome: str
    n: int


class ContactRow(BaseModel):
    channel: str
    guard_verdict: str
    n: int


class CaseRow(BaseModel):
    case_id: uuid.UUID
    entity_id: uuid.UUID
    rank: int | None
    expected_recovery: float | None
    disposition: str
    delinquent_accounts: int
    exposure: float


class MetricBlock(BaseModel):
    title: str
    values: dict[str, Any]
    headline: str | None = None
    caption: str | None = None


class Dashboard(BaseModel):
    totals: list[Count]
    last_resolution_at: datetime | None
    last_screening_at: datetime | None
    account_states: list[StateRow]
    edge_types: list[Count]
    screening: list[ScreeningRow]
    case_dispositions: list[Count]
    # Cases whose entity holds two or more delinquent accounts: one case where the account
    # view would have run that many separate sequences (CLAUDE.md §1).
    collapsed_cases: int
    top_cases: list[CaseRow]
    runtime_contacts: list[ContactRow]
    simulated_contacts: int
    risk_file_events: list[Count]
    metrics: list[MetricBlock] | None


def _count(session: Session, column: Any) -> int:
    return session.scalar(select(func.count(column))) or 0


def _grouped(session: Session, column: Any) -> list[Count]:
    rows = session.execute(select(column, func.count()).group_by(column).order_by(column))
    return [Count(label=str(getattr(key, "value", key)), n=n) for key, n in rows]


def load_metrics(path: Path) -> list[MetricBlock] | None:
    """The report's headline blocks, scalars only. None if the report has not been written."""
    if not path.is_file():
        return None
    report = json.loads(path.read_text())
    blocks = []
    for key, title in METRIC_BLOCKS:
        block = report.get(key)
        if isinstance(block, dict):
            scalars = {k: v for k, v in block.items() if not isinstance(v, dict | list)}
            headline = caption = None
            if key in HEADLINES and isinstance(scalars.get(HEADLINES[key][0]), int | float):
                value_key, style, caption = HEADLINES[key]
                headline = _written(scalars[value_key], style)
            blocks.append(
                MetricBlock(title=title, values=scalars, headline=headline, caption=caption)
            )
    return blocks


def load_dashboard(session: Session, metrics_path: Path) -> Dashboard:
    totals = [
        Count(label="Customer records", n=_count(session, PartyRecord.record_id)),
        Count(label="Resolved entities", n=_count(session, Entity.entity_id)),
        Count(
            label="Entities merging 2+ records",
            n=session.scalar(select(func.count()).where(Entity.cluster_size > 1)) or 0,
        ),
        Count(label="Network links", n=_count(session, EntityEdge.edge_id)),
        Count(label="Accounts", n=_count(session, Account.account_id)),
        Count(
            label="Sanctions listings",
            n=session.scalar(select(func.count()).where(SanctionsEntity.is_current)) or 0,
        ),
        Count(label="Sanctions aliases", n=_count(session, SanctionsAlias.alias_id)),
        Count(label="Collections cases", n=_count(session, Case.case_id)),
    ]

    by_state = {
        state: (n, exposure)
        for state, n, exposure in session.execute(
            select(Account.current_state, func.count(), func.sum(Account.exposure)).group_by(
                Account.current_state
            )
        )
    }
    # Lifecycle order (current, 30, 60, 90, cured, charged off), not the database's sort order.
    account_states = [
        StateRow(state=state.value, accounts=by_state[state][0], exposure=float(by_state[state][1]))
        for state in DelinquencyBucket
        if state in by_state
    ]

    # Decisions against the current sanctions snapshot only: those are the ones the guard
    # counts. Decisions made against an earlier listing are history.
    snapshot_id = current_snapshot_id(session)
    screening = [
        ScreeningRow(method=method.value, outcome=outcome, n=n)
        for method, outcome, n in session.execute(
            select(ScreeningDecision.method, ScreeningDecision.outcome, func.count())
            .where(ScreeningDecision.snapshot_id == snapshot_id)
            .group_by(ScreeningDecision.method, ScreeningDecision.outcome)
            .order_by(ScreeningDecision.method, ScreeningDecision.outcome)
        )
    ]

    delinquent = (
        select(
            Account.entity_id,
            func.count().label("n"),
            func.sum(Account.exposure).label("exposure"),
        )
        .where(Account.current_state.in_(DELINQUENT))
        .group_by(Account.entity_id)
        .subquery()
    )
    collapsed_cases = (
        session.scalar(
            select(func.count())
            .select_from(Case)
            .join(delinquent, delinquent.c.entity_id == Case.entity_id)
            .where(delinquent.c.n >= 2)
        )
        or 0
    )
    top_cases = [
        CaseRow(
            case_id=case.case_id,
            entity_id=case.entity_id,
            rank=case.rank,
            expected_recovery=(
                None if case.expected_recovery is None else float(case.expected_recovery)
            ),
            disposition=case.disposition.value,
            delinquent_accounts=n or 0,
            exposure=float(exposure or 0),
        )
        for case, n, exposure in session.execute(
            select(Case, delinquent.c.n, delinquent.c.exposure)
            .outerjoin(delinquent, delinquent.c.entity_id == Case.entity_id)
            .order_by(Case.rank.asc().nulls_last(), Case.case_id)
            .limit(TOP_CASES)
        )
    ]

    # Read-only split of contact_event by source. Everything that is not simulated history was
    # recorded by the guard's dispatch, the only writer of live contacts (I2).
    runtime_contacts = []
    simulated_contacts = 0
    for source, channel, verdict, n in session.execute(
        select(ContactEvent.source, ContactEvent.channel, ContactEvent.guard_verdict, func.count())
        .group_by(ContactEvent.source, ContactEvent.channel, ContactEvent.guard_verdict)
        .order_by(ContactEvent.source, ContactEvent.channel, ContactEvent.guard_verdict)
    ):
        if source == ContactSource.SIMULATED_HISTORY:
            simulated_contacts += n
        else:
            runtime_contacts.append(
                ContactRow(channel=channel.value, guard_verdict=verdict or "none", n=n)
            )

    return Dashboard(
        totals=totals,
        last_resolution_at=session.scalar(select(func.max(Entity.created_at))),
        last_screening_at=session.scalar(select(func.max(ScreeningDecision.decided_at))),
        account_states=account_states,
        edge_types=_grouped(session, EntityEdge.edge_type),
        screening=screening,
        case_dispositions=_grouped(session, Case.disposition),
        collapsed_cases=collapsed_cases,
        top_cases=top_cases,
        runtime_contacts=runtime_contacts,
        simulated_contacts=simulated_contacts,
        risk_file_events=_grouped(session, RiskFileEvent.event_type),
        metrics=load_metrics(metrics_path),
    )
