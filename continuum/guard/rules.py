"""Deterministic guard rules. No model call anywhere in this module (CLAUDE.md I1, I2).

Each rule is a pure function of GuardInput and Settings returning a RuleResult, so every rule
is exhaustively unit-testable with exact expected outputs (CLAUDE.md §10).

Phase 5 ships these rules so the decisioning graph can reach a real verdict; Phase 6 adds the
LLM tone check, the dispatch worker that is reachable only after a PASS, and the
prompt-injection suite.
"""

from __future__ import annotations

from datetime import datetime

from continuum.config.settings import Settings
from continuum.guard.models import GuardInput, RuleOutcome, RuleResult
from continuum.screening.status import ScreeningState

# Phrases a collections message must never contain: threats, false urgency, third-party
# disclosure. Lower-case substring match. A conservative starting list modelled on common
# debt-collection conduct rules (FDCPA-style); not legal advice and not exhaustive — Phase 6
# extends it and the injection suite attacks it.
PROHIBITED_TERMS: tuple[str, ...] = (
    "arrest",
    "jail",
    "prison",
    "police",
    "sue you",
    "lawsuit",
    "legal action",
    "garnish",
    "seize",
    "your employer",
    "your family",
    "your neighbors",
    "final notice",
    "last chance",
    "or else",
    "you will regret",
)

OPT_OUT_TEXT = "Reply STOP to opt out."
MAX_MESSAGE_CHARS = 600


def _fail(rule_id: str, outcome: RuleOutcome, detail: str) -> RuleResult:
    return RuleResult(rule_id=rule_id, outcome=outcome, detail=detail)


def _ok(rule_id: str) -> RuleResult:
    return RuleResult(rule_id=rule_id, outcome=RuleOutcome.PASS, detail="ok")


def screening_resolved(inp: GuardInput, settings: Settings) -> RuleResult:
    """Hard block unless screening is CLEAR — CLAUDE.md §7, the compliance link the whole
    architecture exists for (a hit or an unresolved screen must never be contacted)."""
    if inp.screening is ScreeningState.CLEAR:
        return _ok("screening_resolved")
    return _fail(
        "screening_resolved",
        RuleOutcome.BLOCK,
        f"screening state is {inp.screening.value}; outreach is not allowed",
    )


def channel_contactable(inp: GuardInput, settings: Settings) -> RuleResult:
    if inp.channel is None:
        return _fail("channel_contactable", RuleOutcome.BLOCK, "no channel was selected")
    reachable = {"sms": inp.has_phone, "email": inp.has_email}.get(inp.channel)
    if reachable is None:
        return _fail("channel_contactable", RuleOutcome.BLOCK, f"unknown channel {inp.channel!r}")
    if not reachable:
        return _fail(
            "channel_contactable", RuleOutcome.BLOCK, f"no {inp.channel} contact point on file"
        )
    return _ok("channel_contactable")


def in_quiet_hours(moment: datetime, settings: Settings) -> bool:
    hour = moment.hour
    start = settings.contact_policy_quiet_hours_start
    end = settings.contact_policy_quiet_hours_end
    if start > end:  # window wraps midnight, e.g. 21:00-08:00
        return hour >= start or hour < end
    return start <= hour < end


def quiet_hours(inp: GuardInput, settings: Settings) -> RuleResult:
    if inp.send_at is None:
        return _fail("quiet_hours", RuleOutcome.BLOCK, "no send time was selected")
    if in_quiet_hours(inp.send_at, settings):
        return _fail(
            "quiet_hours",
            RuleOutcome.BLOCK,
            f"send time {inp.send_at.isoformat()} is in quiet hours",
        )
    return _ok("quiet_hours")


def frequency_cap(inp: GuardInput, settings: Settings) -> RuleResult:
    cap = settings.contact_policy_max_contacts_per_week
    if inp.recent_contacts_7d >= cap:
        return _fail(
            "frequency_cap",
            RuleOutcome.BLOCK,
            f"{inp.recent_contacts_7d} contacts in the last 7 days; cap is {cap}",
        )
    return _ok("frequency_cap")


def prohibited_terms(inp: GuardInput, settings: Settings) -> RuleResult:
    lowered = inp.message_body.lower()
    found = sorted(term for term in PROHIBITED_TERMS if term in lowered)
    if found:
        return _fail("prohibited_terms", RuleOutcome.BLOCK, f"message contains {found}")
    return _ok("prohibited_terms")


def opt_out_present(inp: GuardInput, settings: Settings) -> RuleResult:
    if OPT_OUT_TEXT.lower() not in inp.message_body.lower():
        return _fail("opt_out_present", RuleOutcome.BLOCK, "required opt-out text is missing")
    return _ok("opt_out_present")


def message_length(inp: GuardInput, settings: Settings) -> RuleResult:
    if not inp.message_body.strip():
        return _fail("message_length", RuleOutcome.BLOCK, "message is empty")
    if len(inp.message_body) > MAX_MESSAGE_CHARS:
        return _fail(
            "message_length",
            RuleOutcome.BLOCK,
            f"message is {len(inp.message_body)} chars; limit is {MAX_MESSAGE_CHARS}",
        )
    return _ok("message_length")


def high_exposure(inp: GuardInput, settings: Settings) -> RuleResult:
    if inp.total_overdue > settings.high_exposure_escalation_amount:
        return _fail(
            "high_exposure",
            RuleOutcome.ESCALATE,
            f"total overdue {inp.total_overdue:,.2f} exceeds "
            f"{settings.high_exposure_escalation_amount:,.2f}; a human must approve",
        )
    return _ok("high_exposure")


def upstream_review(inp: GuardInput, settings: Settings) -> RuleResult:
    if inp.upstream_flags:
        return _fail(
            "upstream_review",
            RuleOutcome.ESCALATE,
            f"an earlier step requested review: {sorted(inp.upstream_flags)}",
        )
    return _ok("upstream_review")


ALL_RULES = (
    screening_resolved,
    channel_contactable,
    quiet_hours,
    frequency_cap,
    prohibited_terms,
    opt_out_present,
    message_length,
    high_exposure,
    upstream_review,
)
