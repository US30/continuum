"""route — the case's final destination, and the human-review interrupt.

CLAUDE.md §7: "cleared -> dispatch queue; blocked or high-exposure -> interrupt".

  divert                         -> DIVERTED (network fraud signature; never contacted)
  expected net recovery <= 0     -> no_action (contacting is not worth it, or hurts)
  guard PASS                     -> ready_for_dispatch
  guard ESCALATE / BLOCK         -> interrupt for a human, resume with a ReviewDecision:
        ESCALATE + approve       -> ready_for_dispatch (verdict.human_override = True)
        BLOCK + anything         -> blocked  (a human can acknowledge, never approve sending)
        ESCALATE + reject/ack    -> rejected_by_reviewer

`ready_for_dispatch` only records the decision. Sending is Phase 6's dispatch worker, reachable
only through the guard (I2): nothing in this graph can send.

On resume LangGraph re-runs this node from the top and `interrupt()` returns the human's value,
so everything before it must be free of side effects; all writes happen after it.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from langgraph.types import interrupt
from pydantic import BaseModel
from sqlalchemy.orm import Session

from continuum.db.models.collections import Case, CaseDisposition
from continuum.db.models.risk_file import RiskFileEvent, RiskFileEventType
from continuum.graph.deps import GraphDeps
from continuum.graph.state import CaseState, DispatchRequest, Route
from continuum.guard.engine import apply_human_approval
from continuum.guard.models import GuardVerdict, OutboundMessage, Verdict
from continuum.telemetry.spans import node_span

EVENT_ID_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-0000000000e5")


class ReviewDecision(BaseModel):
    """What a human sends back when resuming an interrupted case."""

    decision: Literal["approve", "reject", "acknowledge"]
    reviewer: str


def _persist(session: Session, state: CaseState, route: Route, override: bool) -> None:
    assert state.case is not None
    case_row = session.get(Case, state.case.case_id)
    if case_row is not None:
        case_row.disposition = CaseDisposition(route.disposition)
        if state.priority is not None:
            case_row.expected_recovery = state.priority.expected_net_recovery
    # Append-only (I3) and idempotent (I6): one event per (case, run, route).
    session.merge(
        RiskFileEvent(
            event_id=uuid.uuid5(EVENT_ID_NAMESPACE, f"{state.case.case_id}:{state.run_id}"),
            entity_id=state.entity_id,
            event_type=RiskFileEventType.CASE_DISPOSITION,
            payload=json.dumps(
                {
                    "case_id": str(state.case.case_id),
                    "run_id": state.run_id,
                    "route": route.route,
                    "disposition": route.disposition,
                    "guard": state.guard.verdict.value if state.guard else None,
                    "human_override": override,
                    "reviewer": route.reviewer,
                    "note": route.note,
                }
            ),
        )
    )


def decide_route(state: CaseState) -> tuple[Route, bool]:
    """Pure part of routing for everything that needs no human. Returns (route, needs_human)."""
    assert state.priority is not None
    if state.priority.divert:
        return (
            Route(
                route="diverted",
                disposition=CaseDisposition.DIVERTED.value,
                note=state.priority.rationale,
            ),
            False,
        )
    if not state.priority.worth_contacting:
        return (
            Route(
                route="no_action",
                disposition=CaseDisposition.OPEN.value,
                note=f"expected net recovery {state.priority.expected_net_recovery:+.2f}",
            ),
            False,
        )
    assert state.guard is not None
    if state.guard.verdict is Verdict.PASS:
        return Route(route="ready_for_dispatch", disposition=CaseDisposition.OPEN.value), False
    return Route(route="blocked", disposition=CaseDisposition.ESCALATED.value), True


def route_case(state: CaseState, deps: GraphDeps) -> dict[str, Any]:
    with node_span("route", state.entity_id):
        if state.route is not None:  # no_delinquency, decided at assembly
            return {"events": ["route: no_delinquency"]}

        route, needs_human = decide_route(state)
        guard = state.guard
        override = False

        if needs_human:
            assert guard is not None and state.case is not None
            decision = ReviewDecision.model_validate(
                interrupt(
                    {
                        "entity_id": str(state.entity_id),
                        "case_id": str(state.case.case_id),
                        "verdict": guard.verdict.value,
                        "reasons": guard.reasons,
                        "total_overdue": state.case.total_overdue,
                        "draft": state.draft.body if state.draft else None,
                        "may_approve": guard.verdict is Verdict.ESCALATE,
                    }
                )
            )
            if guard.verdict is Verdict.ESCALATE and decision.decision == "approve":
                route = Route(
                    route="ready_for_dispatch",
                    disposition=CaseDisposition.OPEN.value,
                    reviewer=decision.reviewer,
                    note="approved by reviewer",
                )
                override = True
            elif guard.verdict is Verdict.ESCALATE:
                route = Route(
                    route="rejected_by_reviewer",
                    disposition=CaseDisposition.ESCALATED.value,
                    reviewer=decision.reviewer,
                    note=f"reviewer decision: {decision.decision}",
                )
            else:  # BLOCK: acknowledge only; approve is ignored, never honoured
                route = Route(
                    route="blocked",
                    disposition=CaseDisposition.ESCALATED.value,
                    reviewer=decision.reviewer,
                    note=f"hard block; reviewer decision {decision.decision!r} cannot send",
                )

        with deps.session_scope() as session:
            _persist(session, state, route, override)

        update: dict[str, Any] = {"route": route, "events": [f"route: {route.route}"]}
        if override and guard is not None:
            # Only the guard can record an approval, and it re-signs the verdict.
            guard = apply_human_approval(guard, route.reviewer or "unknown", deps.settings)
            update["guard"] = guard
        if route.route == "ready_for_dispatch" and guard is not None:
            update["dispatch"] = _dispatch_request(state, guard)
        return update


def _dispatch_request(state: CaseState, guard: GuardVerdict) -> DispatchRequest | None:
    chosen = state.strategy.chosen if state.strategy else None
    if chosen is None or state.draft is None or state.case is None:
        return None
    return DispatchRequest(
        verdict=guard,
        message=OutboundMessage(
            entity_id=state.entity_id,
            case_id=state.case.case_id,
            channel=chosen.channel,
            subject=state.draft.subject,
            body=state.draft.body,
            send_at=chosen.send_at,
        ),
    )
