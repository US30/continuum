"""strategy — channel and timing, chosen from options that are legal by construction.

CLAUDE.md §7: "channel + timing under frequency caps and quiet hours". The options are built
deterministically from what the customer can actually be reached on and from the frequency
cap and quiet-hours rules; the model only *picks one*. An out-of-range pick, or any model
failure, falls back to the first option — the model cannot create an option the rules did not
allow. (The guard re-checks everything independently; this node is convenience, not
enforcement.)
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from continuum.graph.deps import GraphDeps
from continuum.graph.schemas import StrategyChoice
from continuum.graph.state import CaseState, CaseView, SendOption, Strategy
from continuum.guard.rules import in_quiet_hours
from continuum.telemetry.spans import node_span

PROMPT_DIR = Path(__file__).parent.parent / "prompts"
PROMPT_VERSION = "strategy_v1"
_SYSTEM = (PROMPT_DIR / f"{PROMPT_VERSION}_system.txt").read_text(encoding="utf-8")
_USER = (PROMPT_DIR / f"{PROMPT_VERSION}_user.txt").read_text(encoding="utf-8")

# Candidate send hours on the next day, local time. Chosen inside the quiet-hours-free window
# (Settings.contact_policy_quiet_hours_*); anything that lands in quiet hours is dropped anyway.
CANDIDATE_HOURS = (10, 14, 18)


def build_options(case: CaseView, deps: GraphDeps) -> list[SendOption]:
    settings = deps.settings
    if case.recent_contacts_7d >= settings.contact_policy_max_contacts_per_week:
        return []  # frequency cap reached: no legal option; the guard will report it

    channels = [
        channel
        for channel, available in (
            ("sms", case.contact.has_phone),
            ("email", case.contact.has_email),
        )
        if available
    ]
    day = case.as_of + timedelta(days=1)
    slots = [datetime.combine(day, time(hour=h)) for h in CANDIDATE_HOURS]
    return [
        SendOption(channel=channel, send_at=slot)
        for channel in channels
        for slot in slots
        if not in_quiet_hours(slot, settings)
    ]


def choose(
    options: list[SendOption], case: CaseView, deps: GraphDeps, usage: Any
) -> tuple[int, str, bool]:
    """(index, rationale, llm_used). Never raises: falls back to option 0."""
    if len(options) == 1:
        return 0, "only one legal option", False
    listing = "\n".join(
        f"  {i}: {o.channel} at {o.send_at:%Y-%m-%d %H:%M}" for i, o in enumerate(options)
    )
    user = _USER.format(
        total_overdue=f"{case.total_overdue:,.2f}",
        max_dpd=case.max_days_past_due,
        n_accounts=len(case.accounts),
        options=listing,
    )
    try:
        result = deps.llm.complete("strategy", _SYSTEM, user, StrategyChoice)
    except Exception as exc:
        return 0, f"model unavailable ({type(exc).__name__}); used first option", False
    u = result.usage
    usage.record(u.model, u.input_tokens, u.output_tokens, u.cost_usd, u.calls)
    index = result.parsed.option_index
    if not 0 <= index < len(options):
        return 0, f"model chose invalid option {index}; used first option", True
    return index, result.parsed.rationale, True


def plan_strategy(state: CaseState, deps: GraphDeps) -> dict[str, Any]:
    with node_span("strategy", state.entity_id) as usage:
        assert state.case is not None
        options = build_options(state.case, deps)
        if not options:
            strategy = Strategy(
                options=[], chosen=None, rationale="no legal channel/time option available"
            )
        else:
            index, rationale, llm_used = choose(options, state.case, deps, usage)
            strategy = Strategy(
                options=options, chosen=options[index], rationale=rationale, llm_used=llm_used
            )
        chosen = strategy.chosen
        return {
            "strategy": strategy,
            "events": [
                "strategy: "
                + (
                    f"{chosen.channel} at {chosen.send_at:%Y-%m-%d %H:%M}"
                    if chosen
                    else "no option"
                )
            ],
        }
