"""guard node — adapter from graph state to the deterministic rule engine.

All decision logic lives in continuum.guard (mypy --strict, no model calls). This node only
translates state into a GuardInput of plain facts. Note what is *not* passed: nothing a model
said about whether sending is acceptable. The message text is passed because the rules must
inspect it; they treat it as untrusted input.
"""

from __future__ import annotations

from typing import Any

from continuum.graph.deps import GraphDeps
from continuum.graph.state import CaseState
from continuum.guard.engine import evaluate
from continuum.guard.models import GuardInput
from continuum.telemetry.spans import node_span


def guard_case(state: CaseState, deps: GraphDeps) -> dict[str, Any]:
    with node_span("guard", state.entity_id) as usage:
        assert state.case is not None and state.priority is not None
        case, chosen = state.case, state.strategy.chosen if state.strategy else None
        flags = ["fraud_adjudication_unavailable"] if state.priority.needs_review else []
        verdict = evaluate(
            GuardInput(
                entity_id=state.entity_id,
                case_id=case.case_id,
                screening=case.screening,
                total_overdue=case.total_overdue,
                channel=chosen.channel if chosen else None,
                has_phone=case.contact.has_phone,
                has_email=case.contact.has_email,
                send_at=chosen.send_at if chosen else None,
                recent_contacts_7d=case.recent_contacts_7d,
                message_body=state.draft.body if state.draft else "",
                upstream_flags=flags,
            ),
            deps.settings,
            deps.tone,
        )
        if verdict.tone is not None and verdict.tone.checked:
            t = verdict.tone
            usage.record(t.model, t.input_tokens, t.output_tokens, t.cost_usd)
        return {"guard": verdict, "events": [f"guard: {verdict.verdict.value} {verdict.reasons}"]}
