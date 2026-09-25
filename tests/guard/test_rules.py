"""Exhaustive tests for the deterministic guard rules (CLAUDE.md §10: exact expected outputs)."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from continuum.config.settings import Settings
from continuum.guard import rules
from continuum.guard.engine import evaluate
from continuum.guard.models import GuardInput, RuleOutcome, ToneResult, Verdict
from continuum.screening.status import ScreeningState

SETTINGS = Settings()
ENTITY_ID = uuid.uuid4()


class OkTone:
    def check(self, message: str, channel: str) -> ToneResult:
        return ToneResult(checked=True, acceptable=True)


GOOD_BODY = (
    "Hi Sam, your payment of $120.00 is overdue. Please pay when you can. Reply STOP to opt out."
)


def make(**overrides: object) -> GuardInput:
    base: dict[str, object] = {
        "entity_id": ENTITY_ID,
        "screening": ScreeningState.CLEAR,
        "total_overdue": 500.0,
        "channel": "sms",
        "has_phone": True,
        "has_email": True,
        "send_at": datetime(2026, 1, 2, 10, 0),
        "recent_contacts_7d": 0,
        "message_body": GOOD_BODY,
        "upstream_flags": [],
    }
    base.update(overrides)
    return GuardInput.model_validate(base)


class TestScreeningResolved:
    def test_clear_passes(self) -> None:
        assert rules.screening_resolved(make(), SETTINGS).outcome is RuleOutcome.PASS

    @pytest.mark.parametrize("state", [ScreeningState.HIT, ScreeningState.UNRESOLVED])
    def test_hit_or_unresolved_blocks(self, state: ScreeningState) -> None:
        assert (
            rules.screening_resolved(make(screening=state), SETTINGS).outcome is RuleOutcome.BLOCK
        )


class TestChannel:
    def test_sms_with_phone_passes(self) -> None:
        assert rules.channel_contactable(make(), SETTINGS).outcome is RuleOutcome.PASS

    def test_email_with_email_passes(self) -> None:
        inp = make(channel="email", has_phone=False)
        assert rules.channel_contactable(inp, SETTINGS).outcome is RuleOutcome.PASS

    def test_sms_without_phone_blocks(self) -> None:
        assert (
            rules.channel_contactable(make(has_phone=False), SETTINGS).outcome is RuleOutcome.BLOCK
        )

    def test_no_channel_blocks(self) -> None:
        assert rules.channel_contactable(make(channel=None), SETTINGS).outcome is RuleOutcome.BLOCK

    def test_unknown_channel_blocks(self) -> None:
        assert rules.channel_contactable(make(channel="fax"), SETTINGS).outcome is RuleOutcome.BLOCK


class TestQuietHours:
    @pytest.mark.parametrize("hour", [8, 9, 12, 20])
    def test_daytime_passes(self, hour: int) -> None:
        inp = make(send_at=datetime(2026, 1, 2, hour, 0))
        assert rules.quiet_hours(inp, SETTINGS).outcome is RuleOutcome.PASS

    @pytest.mark.parametrize("hour", [21, 23, 0, 3, 7])
    def test_night_blocks(self, hour: int) -> None:
        inp = make(send_at=datetime(2026, 1, 2, hour, 0))
        assert rules.quiet_hours(inp, SETTINGS).outcome is RuleOutcome.BLOCK

    def test_missing_send_time_blocks(self) -> None:
        assert rules.quiet_hours(make(send_at=None), SETTINGS).outcome is RuleOutcome.BLOCK


class TestFrequencyCap:
    def test_below_cap_passes(self) -> None:
        cap = SETTINGS.contact_policy_max_contacts_per_week
        assert rules.frequency_cap(make(recent_contacts_7d=cap - 1), SETTINGS).outcome is (
            RuleOutcome.PASS
        )

    def test_at_cap_blocks(self) -> None:
        cap = SETTINGS.contact_policy_max_contacts_per_week
        assert rules.frequency_cap(make(recent_contacts_7d=cap), SETTINGS).outcome is (
            RuleOutcome.BLOCK
        )


class TestMessageRules:
    def test_clean_message_passes_all_content_rules(self) -> None:
        inp = make()
        for rule in (rules.prohibited_terms, rules.opt_out_present, rules.message_length):
            assert rule(inp, SETTINGS).outcome is RuleOutcome.PASS

    @pytest.mark.parametrize("term", list(rules.PROHIBITED_TERMS))
    def test_every_prohibited_term_blocks_case_insensitively(self, term: str) -> None:
        inp = make(message_body=f"Please pay. {term.upper()}. {rules.OPT_OUT_TEXT}")
        assert rules.prohibited_terms(inp, SETTINGS).outcome is RuleOutcome.BLOCK

    def test_missing_opt_out_blocks(self) -> None:
        inp = make(message_body="Please pay your overdue balance.")
        assert rules.opt_out_present(inp, SETTINGS).outcome is RuleOutcome.BLOCK

    def test_opt_out_match_is_case_insensitive(self) -> None:
        inp = make(message_body="Pay soon. reply stop to opt out.")
        assert rules.opt_out_present(inp, SETTINGS).outcome is RuleOutcome.PASS

    def test_empty_and_overlong_messages_block(self) -> None:
        assert rules.message_length(make(message_body="  "), SETTINGS).outcome is RuleOutcome.BLOCK
        long = "x" * (rules.MAX_MESSAGE_CHARS + 1)
        assert rules.message_length(make(message_body=long), SETTINGS).outcome is RuleOutcome.BLOCK


class TestEscalations:
    def test_high_exposure_escalates_above_threshold_only(self) -> None:
        limit = SETTINGS.high_exposure_escalation_amount
        assert rules.high_exposure(make(total_overdue=limit), SETTINGS).outcome is RuleOutcome.PASS
        over = make(total_overdue=limit + 0.01)
        assert rules.high_exposure(over, SETTINGS).outcome is RuleOutcome.ESCALATE

    def test_upstream_flag_escalates(self) -> None:
        inp = make(upstream_flags=["fraud_adjudication_failed"])
        assert rules.upstream_review(inp, SETTINGS).outcome is RuleOutcome.ESCALATE


class TestEngine:
    def test_all_clear_passes(self) -> None:
        verdict = evaluate(make(), SETTINGS, OkTone())
        assert verdict.verdict is Verdict.PASS and verdict.reasons == []
        assert len(verdict.rules) == len(rules.ALL_RULES) + 1  # + tone check

    def test_any_block_wins_over_escalate(self) -> None:
        inp = make(total_overdue=1e6, screening=ScreeningState.HIT)
        assert evaluate(inp, SETTINGS).verdict is Verdict.BLOCK

    def test_escalate_only_when_no_block(self) -> None:
        assert evaluate(make(total_overdue=1e6), SETTINGS).verdict is Verdict.ESCALATE

    def test_every_rule_reports_even_after_a_block(self) -> None:
        inp = make(screening=ScreeningState.UNRESOLVED, send_at=datetime(2026, 1, 2, 23, 0))
        verdict = evaluate(inp, SETTINGS)
        failed = {r.rule_id for r in verdict.rules if r.outcome is RuleOutcome.BLOCK}
        assert {"screening_resolved", "quiet_hours"} <= failed

    def test_a_hit_cannot_be_overridden(self) -> None:
        verdict = evaluate(make(screening=ScreeningState.HIT), SETTINGS)
        assert verdict.verdict is Verdict.BLOCK and verdict.human_override is False


class TestObfuscationResistantMatching:
    @pytest.mark.parametrize(
        "text",
        [
            "or we will ARREST you",
            "or we will arr\u200best you",  # zero-width space
            "or we will a r r e s t you",
            "or we will a-r-r-e-s-t you",
            "or we will 4rr3st you",
            "or we call the p\u043elice",  # Cyrillic o
            "you will be arrested",
            "our attorney will call",
            "see you in court",
        ],
    )
    def test_obfuscated_prohibited_terms_are_still_caught(self, text: str) -> None:
        inp = make(message_body=f"Please pay. {text}. {rules.OPT_OUT_TEXT}")
        assert rules.prohibited_terms(inp, SETTINGS).outcome is RuleOutcome.BLOCK

    @pytest.mark.parametrize(
        "text",
        [
            "As a courtesy we wanted to remind you",
            "The carrest report was fine",
            "Your balance is 120 dollars",
            "We appreciate your business and are happy to help",
        ],
    )
    def test_ordinary_text_is_not_flagged(self, text: str) -> None:
        inp = make(message_body=f"{text}. {rules.OPT_OUT_TEXT}")
        assert rules.prohibited_terms(inp, SETTINGS).outcome is RuleOutcome.PASS
        assert rules.injection_markers(inp, SETTINGS).outcome is RuleOutcome.PASS

    @pytest.mark.parametrize("marker", list(rules.INJECTION_MARKERS))
    def test_every_injection_marker_blocks(self, marker: str) -> None:
        inp = make(message_body=f"Please pay. {marker.upper()} thanks. {rules.OPT_OUT_TEXT}")
        assert rules.injection_markers(inp, SETTINGS).outcome is RuleOutcome.BLOCK
