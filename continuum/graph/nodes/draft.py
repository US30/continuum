"""draft — a tone-matched message. The model writes the body; code owns everything the rules
depend on.

The opt-out line is appended by code (never trusted to the model), and if the model is
unavailable a fixed neutral template is used, so this node always yields a message the guard
can evaluate. The guard, not this node, decides whether the text may be sent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from continuum.graph.deps import GraphDeps
from continuum.graph.schemas import DraftOutput
from continuum.graph.state import CaseState, CaseView, Draft
from continuum.guard.rules import OPT_OUT_TEXT
from continuum.telemetry.spans import node_span

PROMPT_DIR = Path(__file__).parent.parent / "prompts"
PROMPT_VERSION = "draft_v1"
_SYSTEM = (PROMPT_DIR / f"{PROMPT_VERSION}_system.txt").read_text(encoding="utf-8")
_USER = (PROMPT_DIR / f"{PROMPT_VERSION}_user.txt").read_text(encoding="utf-8")

EMAIL_SUBJECT = "About your account balance"


def fallback_body(case: CaseView) -> str:
    return (
        f"Hi {case.contact.first_name}, our records show {case.total_overdue:,.2f} is overdue on "
        "your account. If you have already paid, thank you. If you need help or want to talk "
        "through options, please get in touch."
    )


def with_opt_out(body: str) -> str:
    body = body.strip()
    if OPT_OUT_TEXT.lower() in body.lower():
        return body
    return f"{body} {OPT_OUT_TEXT}"


def write_draft(state: CaseState, deps: GraphDeps) -> dict[str, Any]:
    with node_span("draft", state.entity_id) as usage:
        assert state.case is not None and state.strategy is not None
        case, chosen = state.case, state.strategy.chosen
        if chosen is None:
            return {"events": ["draft: skipped, no channel chosen"]}

        user = _USER.format(
            first_name=case.contact.first_name,
            channel=chosen.channel,
            total_overdue=f"{case.total_overdue:,.2f}",
            n_accounts=len(case.accounts),
            products=", ".join(sorted({a.product.replace("_", " ") for a in case.accounts})),
            max_dpd=case.max_days_past_due,
        )
        llm_used = False
        try:
            result = deps.llm.complete("draft", _SYSTEM, user, DraftOutput)
            u = result.usage
            usage.record(u.model, u.input_tokens, u.output_tokens, u.cost_usd, u.calls)
            body = result.parsed.body
            llm_used = True
        except Exception:
            body = fallback_body(case)

        draft = Draft(
            channel=chosen.channel,
            subject=EMAIL_SUBJECT if chosen.channel == "email" else None,
            body=with_opt_out(body),
            prompt_version=PROMPT_VERSION,
            llm_used=llm_used,
        )
        return {"draft": draft, "events": [f"draft: {len(draft.body)} chars, llm={llm_used}"]}
