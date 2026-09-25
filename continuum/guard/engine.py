"""Runs the deterministic rules, then (only if nothing blocked) the tone check, and signs the
result.

Every deterministic rule always runs (no short-circuit), so a blocked message reports *all*
reasons — what a human reviewer and the audit trail need. Combination: any BLOCK -> BLOCK;
else any ESCALATE -> ESCALATE; else PASS. The tone check runs only when no rule BLOCKed, and
can only add an ESCALATE (CLAUDE.md §7: deterministic rules first, so a prompt injection cannot
unblock a send).

`apply_human_approval` is the only way a verdict acquires `human_override`, and it refuses
anything but ESCALATE — a BLOCK stays a BLOCK.
"""

from __future__ import annotations

from datetime import UTC, datetime

from continuum.config.settings import Settings
from continuum.guard import signing
from continuum.guard.models import (
    GuardInput,
    GuardVerdict,
    RuleOutcome,
    RuleResult,
    ToneResult,
    Verdict,
)
from continuum.guard.rules import ALL_RULES
from continuum.guard.tone import ToneChecker, unavailable


class ApprovalRefusedError(RuntimeError):
    """A human tried to approve something the guard does not allow to be approved."""


def _tone_rule(inp: GuardInput, tone: ToneChecker | None) -> tuple[RuleResult, ToneResult]:
    result = unavailable("no tone checker configured") if tone is None else _safe_check(inp, tone)
    if not result.checked:
        return (
            RuleResult(rule_id="tone_check", outcome=RuleOutcome.ESCALATE, detail=result.reason),
            result,
        )
    if not result.acceptable:
        return (
            RuleResult(
                rule_id="tone_check",
                outcome=RuleOutcome.ESCALATE,
                detail=f"tone not acceptable: {result.issues}",
            ),
            result,
        )
    return RuleResult(rule_id="tone_check", outcome=RuleOutcome.PASS, detail="ok"), result


def _safe_check(inp: GuardInput, tone: ToneChecker) -> ToneResult:
    try:
        return tone.check(inp.message_body, inp.channel or "unknown")
    except Exception as exc:  # a checker bug must never become a PASS
        return unavailable(type(exc).__name__)


def evaluate(
    inp: GuardInput,
    settings: Settings,
    tone: ToneChecker | None = None,
    now: datetime | None = None,
) -> GuardVerdict:
    results = [rule(inp, settings) for rule in ALL_RULES]
    tone_result: ToneResult | None = None
    if not any(r.outcome is RuleOutcome.BLOCK for r in results):
        tone_rule, tone_result = _tone_rule(inp, tone)
        results.append(tone_rule)

    outcomes = {r.outcome for r in results}
    if RuleOutcome.BLOCK in outcomes:
        verdict = Verdict.BLOCK
    elif RuleOutcome.ESCALATE in outcomes:
        verdict = Verdict.ESCALATE
    else:
        verdict = Verdict.PASS

    unsigned = GuardVerdict(
        verdict=verdict,
        rules=results,
        entity_id=inp.entity_id,
        case_id=inp.case_id,
        channel=inp.channel,
        send_at=inp.send_at,
        message_hash=signing.message_hash(inp.message_body),
        issued_at=now or datetime.now(UTC),
        tone=tone_result,
    )
    return signing.sign(unsigned, settings.guard_signing_key)


def apply_human_approval(
    verdict: GuardVerdict, reviewer: str, settings: Settings, now: datetime | None = None
) -> GuardVerdict:
    """Record a human's approval of an ESCALATE verdict and re-sign it. Refuses BLOCK and PASS
    (nothing to approve), and refuses a verdict whose signature does not verify."""
    if not signing.verify(verdict, settings.guard_signing_key):
        raise ApprovalRefusedError("verdict signature is invalid")
    if verdict.verdict is not Verdict.ESCALATE:
        raise ApprovalRefusedError(f"a {verdict.verdict.value} verdict cannot be approved")
    approved = verdict.model_copy(
        update={
            "human_override": True,
            "approved_by": reviewer,
            "issued_at": now or datetime.now(UTC),
        }
    )
    return signing.sign(approved, settings.guard_signing_key)
