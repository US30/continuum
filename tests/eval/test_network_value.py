"""Metric 3 logic on tiny hand-built cases with known answers."""

from __future__ import annotations

import uuid

import pytest

from continuum.graph.state import LinkedEntity
from eval.network_value import CaseInput, Truth, account_view_groups, compute_metric3, evaluate_case

P1, P2, P3, P4 = "p1", "p2", "p3", "p4"


def truth(**kw: object) -> Truth:
    base: dict[str, object] = {
        "account_record": {"a1": "r1", "a2": "r2", "a3": "r3"},
        "record_person": {"r1": P1, "r2": P1, "r3": P2, "rl": P3, "rl2": P4},
        "person_group": {},
    }
    base.update(kw)
    return Truth(**base)  # type: ignore[arg-type]


def link(edge_type: str, delinquent: bool = True, entity: str | None = None) -> LinkedEntity:
    return LinkedEntity(
        entity_id=uuid.UUID(entity) if entity else uuid.uuid4(),
        edge_type=edge_type,
        strength=1.0,
        delinquent=delinquent,
    )


def case(
    ids: dict[str, str | None],
    links: tuple[LinkedEntity, ...] = (),
    people: frozenset[str] = frozenset({P1}),
    linked_people: dict[str, frozenset[str]] | None = None,
) -> CaseInput:
    return CaseInput(
        entity_id="e1",
        account_ids=tuple(ids),
        account_national_id=ids,
        account_record={"a1": "r1", "a2": "r2", "a3": "r3"},
        links=links,
        people=people,
        linked_people=linked_people or {},
    )


class TestAccountViewBaseline:
    def test_same_exact_national_id_is_linked(self) -> None:
        groups = account_view_groups(case({"a1": "123-45-6789", "a2": "123-45-6789"}))
        assert len(set(groups.values())) == 1

    def test_formatting_drift_or_a_blank_id_defeats_the_account_view(self) -> None:
        drift = account_view_groups(case({"a1": "123-45-6789", "a2": "123456789"}))
        blank = account_view_groups(case({"a1": None, "a2": None}))
        assert len(set(drift.values())) == 2 and len(set(blank.values())) == 2


class TestLinkedAccount:
    def test_entity_view_beats_account_view_when_ids_drift(self) -> None:
        result = evaluate_case(case({"a1": "123-45-6789", "a2": "123456789"}), truth())
        assert result.linked_account and result.linked_account_valid

    def test_no_gain_when_the_account_view_already_linked_them(self) -> None:
        result = evaluate_case(case({"a1": "123-45-6789", "a2": "123-45-6789"}), truth())
        assert not result.linked_account

    def test_a_single_account_case_has_nothing_to_link(self) -> None:
        assert not evaluate_case(case({"a1": None}), truth()).linked_account

    def test_a_false_merge_counts_raw_but_never_validated(self) -> None:
        # a1 (person p1) and a3 (person p2) wrongly resolved into one entity.
        result = evaluate_case(case({"a1": None, "a3": None}), truth())
        assert result.linked_account and not result.linked_account_valid


class TestControllingParty:
    def _groups(self) -> dict[str, tuple[str, str]]:
        return {P1: ("household-1", "household"), P3: ("household-1", "household")}

    def test_a_strong_link_to_a_delinquent_household_member_is_valid(self) -> None:
        neighbour = str(uuid.uuid4())
        c = case(
            {"a1": None},
            links=(link("shared_address", entity=neighbour),),
            linked_people={neighbour: frozenset({P3})},
        )
        result = evaluate_case(c, truth(person_group=self._groups()))
        assert result.controlling_party and result.controlling_party_valid

    def test_a_coincidental_link_is_raw_but_not_valid(self) -> None:
        neighbour = str(uuid.uuid4())
        c = case(
            {"a1": None},
            links=(link("shared_phone", entity=neighbour),),
            linked_people={neighbour: frozenset({P4})},
        )
        result = evaluate_case(c, truth(person_group=self._groups()))
        assert result.controlling_party and not result.controlling_party_valid

    @pytest.mark.parametrize(
        "weak", [link("name_similarity"), link("shared_address", delinquent=False)]
    )
    def test_weak_or_non_delinquent_links_do_not_count(self, weak: LinkedEntity) -> None:
        assert not evaluate_case(case({"a1": None}, links=(weak,)), truth()).controlling_party


class TestFraudSignature:
    def _ring_case(self, in_ring: bool) -> tuple[CaseInput, Truth]:
        links = tuple(link("shared_phone") for _ in range(3))
        groups = {P1: ("ring-1", "ring")} if in_ring else {P1: ("household-1", "household")}
        return case({"a1": None}, links=links), truth(person_group=groups)

    def test_a_real_ring_is_flagged_and_validated(self) -> None:
        c, t = self._ring_case(in_ring=True)
        result = evaluate_case(c, t)
        assert result.fraud_signature and result.fraud_signature_valid and result.is_ring_case

    def test_a_household_that_trips_the_score_is_raw_only(self) -> None:
        c, t = self._ring_case(in_ring=False)
        result = evaluate_case(c, t)
        assert result.fraud_signature and not result.fraud_signature_valid


class TestReport:
    def test_shares_and_ring_quality(self) -> None:
        ring_links = tuple(link("shared_phone") for _ in range(3))
        cases = [
            case({"a1": "1", "a2": "2"}),  # L valid
            case({"a1": None}, links=ring_links),  # F (ring)
            case({"a1": None}),  # nothing
            case({"a1": None}),  # nothing
        ]
        t = truth(person_group={P1: ("ring-1", "ring")})
        report = compute_metric3(cases, t)
        assert report.n_cases == 4 and report.validated_share == pytest.approx(0.5)
        assert report.raw_share >= report.validated_share
        assert report.ring_cases == 4 and report.ring_cases_flagged == 1

    def test_outreach_saved_is_accounts_minus_cases(self) -> None:
        report = compute_metric3([case({"a1": None, "a2": None, "a3": None})], truth())
        assert report.n_delinquent_accounts == 3 and report.outreach_saved == 2
