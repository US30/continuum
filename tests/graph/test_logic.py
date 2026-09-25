"""Pure-logic tests for the graph's deterministic pieces. No database, no model."""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from continuum.config.settings import Settings
from continuum.graph.nodes.draft import fallback_body, with_opt_out
from continuum.graph.nodes.prioritise import (
    expected_incremental_recovery,
    fraud_band,
    fraud_signals,
)
from continuum.graph.nodes.route import decide_route
from continuum.graph.nodes.strategy import build_options
from continuum.graph.state import (
    AccountView,
    CaseState,
    CaseView,
    ContactPoints,
    LinkedEntity,
    Priority,
)
from continuum.guard.models import GuardVerdict, RuleOutcome, RuleResult, Verdict
from continuum.guard.rules import OPT_OUT_TEXT
from continuum.screening.status import ScreeningState

SETTINGS = Settings()


def link(edge_type: str, delinquent: bool, entity: uuid.UUID | None = None) -> LinkedEntity:
    return LinkedEntity(
        entity_id=entity or uuid.uuid4(), edge_type=edge_type, strength=1.0, delinquent=delinquent
    )


def account(overdue: float = 100.0, uplift: float | None = 0.2) -> AccountView:
    return AccountView(
        account_id=uuid.uuid4(),
        product="personal_loan",
        exposure=1000.0,
        state="DPD_30",
        days_past_due=35,
        overdue_amount=overdue,
        uplift=uplift,
    )


def case(**kw: object) -> CaseView:
    base: dict[str, object] = {
        "entity_id": uuid.uuid4(),
        "case_id": uuid.uuid4(),
        "as_of": date(2026, 1, 1),
        "accounts": [account()],
        "linked_entities": [],
        "screening": ScreeningState.CLEAR,
        "contact": ContactPoints(first_name="Sam", has_phone=True, has_email=True),
        "recent_contacts_7d": 0,
    }
    base.update(kw)
    return CaseView.model_validate(base)


class TestFraudScore:
    def test_no_links_scores_zero(self) -> None:
        assert fraud_signals([])[0] == 0.0

    def test_weak_links_never_count(self) -> None:
        score, strong, _, weak = fraud_signals([link("name_similarity", True) for _ in range(5)])
        assert (score, strong, weak) == (0.0, 0, 5)

    def test_a_household_is_below_the_review_band(self) -> None:
        score = fraud_signals([link("shared_address", False)])[0]
        assert fraud_band(score, SETTINGS) == "clear"

    def test_three_delinquent_strong_links_is_a_signature(self) -> None:
        score = fraud_signals([link("shared_phone", True) for _ in range(3)])[0]
        assert score == 1.0 and fraud_band(score, SETTINGS) == "signature"

    def test_a_middling_ring_is_ambiguous(self) -> None:
        score = fraud_signals([link("shared_address", True), link("shared_address", False)])[0]
        assert fraud_band(score, SETTINGS) == "ambiguous"

    def test_two_edge_types_to_one_neighbour_count_once(self) -> None:
        same = uuid.uuid4()
        _, strong, delinquent, _ = fraud_signals(
            [link("shared_address", True, same), link("shared_phone", True, same)]
        )
        assert (strong, delinquent) == (1, 1)

    def test_band_boundaries(self) -> None:
        assert fraud_band(SETTINGS.fraud_score_review - 0.01, SETTINGS) == "clear"
        assert fraud_band(SETTINGS.fraud_score_review, SETTINGS) == "ambiguous"
        assert fraud_band(SETTINGS.fraud_score_divert, SETTINGS) == "signature"


class TestExpectedRecovery:
    def test_sums_uplift_times_overdue_across_all_accounts(self) -> None:
        c = case(accounts=[account(100, 0.2), account(300, 0.1)])
        assert expected_incremental_recovery(c) == pytest.approx(0.2 * 100 + 0.1 * 300)

    def test_unscored_accounts_contribute_nothing(self) -> None:
        assert expected_incremental_recovery(case(accounts=[account(500, None)])) == 0.0

    def test_negative_uplift_makes_the_case_not_worth_contacting(self) -> None:
        assert expected_incremental_recovery(case(accounts=[account(500, -0.1)])) < 0

    def test_collapse_total_overdue_and_dpd(self) -> None:
        c = case(accounts=[account(100), account(250), account(50)])
        assert c.total_overdue == 400 and len(c.accounts) == 3 and c.max_days_past_due == 35


class TestStrategyOptions:
    def test_channels_follow_available_contact_points(self, plain_deps) -> None:  # type: ignore[no-untyped-def]
        both = build_options(case(), plain_deps)
        assert {o.channel for o in both} == {"sms", "email"}
        only_email = case(contact=ContactPoints(first_name="S", has_phone=False, has_email=True))
        assert {o.channel for o in build_options(only_email, plain_deps)} == {"email"}

    def test_no_contact_points_means_no_options(self, plain_deps) -> None:  # type: ignore[no-untyped-def]
        nobody = case(contact=ContactPoints(first_name="S", has_phone=False, has_email=False))
        assert build_options(nobody, plain_deps) == []

    def test_frequency_cap_removes_every_option(self, plain_deps) -> None:  # type: ignore[no-untyped-def]
        cap = SETTINGS.contact_policy_max_contacts_per_week
        assert build_options(case(recent_contacts_7d=cap), plain_deps) == []

    def test_no_option_is_in_quiet_hours(self, plain_deps) -> None:  # type: ignore[no-untyped-def]
        assert all(8 <= o.send_at.hour < 21 for o in build_options(case(), plain_deps))


class TestDraftHelpers:
    def test_opt_out_is_always_appended_once(self) -> None:
        assert with_opt_out("Pay soon.").endswith(OPT_OUT_TEXT)
        already = f"Pay soon. {OPT_OUT_TEXT}"
        assert with_opt_out(already) == already

    def test_fallback_body_states_the_amount_and_never_threatens(self) -> None:
        body = fallback_body(case(accounts=[account(1234.5)]))
        assert "1,234.50" in body and "Sam" in body


def _state(priority: Priority, verdict: Verdict | None) -> CaseState:
    guard = None
    if verdict is not None:
        outcome = {Verdict.PASS: RuleOutcome.PASS}.get(verdict, RuleOutcome(verdict.value))
        guard = GuardVerdict(
            verdict=verdict, rules=[RuleResult(rule_id="r", outcome=outcome, detail="d")]
        )
    return CaseState(
        entity_id=uuid.uuid4(), as_of=date(2026, 1, 1), run_id="t", priority=priority, guard=guard
    )


def _priority(net: float = 10.0, divert: bool = False) -> Priority:
    return Priority(
        expected_incremental_recovery=net + 1.5,
        contact_cost=1.5,
        expected_net_recovery=net,
        fraud_score=0.0,
        fraud_band="clear",
        divert=divert,
        rationale="r",
    )


class TestDecideRoute:
    def test_divert_wins_over_everything(self) -> None:
        route, human = decide_route(_state(_priority(divert=True), Verdict.PASS))
        assert route.route == "diverted" and not human

    def test_non_positive_net_recovery_is_no_action(self) -> None:
        for net in (0.0, -5.0):
            route, human = decide_route(_state(_priority(net=net), Verdict.PASS))
            assert route.route == "no_action" and not human

    def test_pass_is_ready_for_dispatch(self) -> None:
        route, human = decide_route(_state(_priority(), Verdict.PASS))
        assert route.route == "ready_for_dispatch" and not human

    @pytest.mark.parametrize("verdict", [Verdict.ESCALATE, Verdict.BLOCK])
    def test_escalate_and_block_need_a_human(self, verdict: Verdict) -> None:
        assert decide_route(_state(_priority(), verdict))[1] is True
