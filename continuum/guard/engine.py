"""Runs every deterministic rule and combines them into one verdict.

Every rule always runs (no short-circuit), so a blocked message reports *all* reasons, which
is what a human reviewer and the audit trail need. Combination: any BLOCK -> BLOCK; else any
ESCALATE -> ESCALATE; else PASS.
"""

from __future__ import annotations

from continuum.config.settings import Settings
from continuum.guard.models import GuardInput, GuardVerdict, RuleOutcome, Verdict
from continuum.guard.rules import ALL_RULES


def evaluate(inp: GuardInput, settings: Settings) -> GuardVerdict:
    results = [rule(inp, settings) for rule in ALL_RULES]
    outcomes = {r.outcome for r in results}
    if RuleOutcome.BLOCK in outcomes:
        verdict = Verdict.BLOCK
    elif RuleOutcome.ESCALATE in outcomes:
        verdict = Verdict.ESCALATE
    else:
        verdict = Verdict.PASS
    return GuardVerdict(verdict=verdict, rules=results)
