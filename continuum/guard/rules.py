"""Deterministic guard rules. No model call anywhere in this module (CLAUDE.md I1, I2).

Each rule is a pure function of GuardInput and Settings returning a RuleResult, so every rule
is exhaustively unit-testable with exact expected outputs (CLAUDE.md §10).

Phase 5 ships these rules so the decisioning graph can reach a real verdict; Phase 6 adds the
LLM tone check, the dispatch worker that is reachable only after a PASS, and the
prompt-injection suite.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from continuum.config.settings import Settings
from continuum.guard.confusables import CONFUSABLES
from continuum.guard.models import GuardInput, RuleOutcome, RuleResult
from continuum.screening.status import ScreeningState

# Phrases a collections message must never contain: threats, false urgency, third-party
# disclosure. Matched on a *normalised* form of the text (see `match_variants`), so casing,
# accents, look-alike letters, zero-width characters, l33t substitutions and letter-spacing
# (`a r r e s t`) do not get around them. A conservative starting list modelled on common
# debt-collection conduct rules (FDCPA-style); not legal advice and not exhaustive. A novel
# paraphrase that is in no list is exactly what the LLM tone check exists to catch.
PROHIBITED_TERMS: tuple[str, ...] = (
    "arrest",
    "jail",
    "prison",
    "police",
    "sue you",
    "lawsuit",
    "legal action",
    "legal proceedings",
    "attorney",
    "lawyer",
    "court date",
    "in court",
    "to court",
    "criminal",
    "warrant",
    "sheriff",
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

# Text that only an injected instruction would put in a customer message. If a drafting model
# was talked into echoing guard- or model-directed meta-language, that is itself a reason to
# block, whatever else the message says.
INJECTION_MARKERS: tuple[str, ...] = (
    "ignore previous",
    "ignore all",
    "ignore the above",
    "disregard",
    "system prompt",
    "system:",
    "verdict",
    "untrusted_data",
    "pre-approved",
    "as an ai",
    "```",
)

_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"}
)
_SPACED_LETTERS = re.compile(r"(?<![a-z0-9])(?:[a-z][\W_]+){2,}[a-z](?![a-z0-9])")


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(
        ch for ch in text if unicodedata.category(ch) not in {"Cf", "Cc", "Mn"} or ch in "\n\t"
    )
    return text.casefold().translate(CONFUSABLES)


def match_variants(text: str) -> list[str]:
    """Forms of `text` a rule should inspect: normalised, l33t-decoded, and with spaced-out
    single letters rejoined. A term found in any of them counts."""
    base = _normalise(text)
    leet = base.translate(_LEET)
    joined = _SPACED_LETTERS.sub(lambda m: re.sub(r"[\W_]+", "", m.group(0)), base)
    return [base, leet, joined]


def _found(terms: tuple[str, ...], text: str) -> list[str]:
    variants = match_variants(text)
    # A leading boundary stops `arrest` matching inside `carrest`; a trailing one is omitted
    # on purpose so `arrested` and `arrests` still match.
    return sorted(
        term
        for term in terms
        if any(re.search(rf"(?<![a-z]){re.escape(term)}", variant) for variant in variants)
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
    found = _found(PROHIBITED_TERMS, inp.message_body)
    if found:
        return _fail("prohibited_terms", RuleOutcome.BLOCK, f"message contains {found}")
    return _ok("prohibited_terms")


def injection_markers(inp: GuardInput, settings: Settings) -> RuleResult:
    found = _found(INJECTION_MARKERS, inp.message_body)
    if found:
        return _fail(
            "injection_markers",
            RuleOutcome.BLOCK,
            f"message contains model/guard-directed text {found}",
        )
    return _ok("injection_markers")


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
    injection_markers,
    opt_out_present,
    message_length,
    high_exposure,
    upstream_review,
)
