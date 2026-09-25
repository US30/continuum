"""Pure tests for ScreeningCandidate's derived fields. No DB."""

from __future__ import annotations

import json
from datetime import date

from continuum.screening.candidates import DobEvidence, ScreeningCandidate, dob_evidence


def _candidate(**overrides: object) -> ScreeningCandidate:
    defaults = dict(
        entity_id=None,
        listing_id="synth-0001",
        listing_primary_name="Aldric Vantessen",
        matched_alias="Aldric Vantessen",
        entity_name="Aldric Vantessen",
        entity_date_of_birth=date(1958, 3, 12),
        listing_birth_date=date(1958, 3, 12),
        listing_countries="zz",
        listing_topics="sanction",
        name_similarity=0.97,
    )
    defaults.update(overrides)
    return ScreeningCandidate(**defaults)  # type: ignore[arg-type]


class TestDobMatch:
    def test_matching_dobs(self) -> None:
        assert _candidate().dob_match is True

    def test_mismatched_dobs(self) -> None:
        candidate = _candidate(listing_birth_date=date(1990, 1, 1))
        assert candidate.dob_match is False

    def test_missing_entity_dob(self) -> None:
        candidate = _candidate(entity_date_of_birth=None)
        assert candidate.dob_match is False

    def test_missing_listing_dob(self) -> None:
        candidate = _candidate(listing_birth_date=None)
        assert candidate.dob_match is False

    def test_both_missing(self) -> None:
        candidate = _candidate(entity_date_of_birth=None, listing_birth_date=None)
        assert candidate.dob_match is False


class TestAsEvidence:
    def test_contains_expected_keys(self) -> None:
        evidence = _candidate().as_evidence()
        assert evidence["listing_id"] == "synth-0001"
        assert evidence["dob_evidence"] == "match"
        assert evidence["name_similarity"] == 0.97

    def test_is_json_serialisable(self) -> None:
        json.dumps(_candidate().as_evidence())


class TestDobEvidence:
    def test_equal(self) -> None:
        assert dob_evidence(date(1958, 3, 12), date(1958, 3, 12)) is DobEvidence.MATCH

    def test_one_day_off_is_a_match(self) -> None:  # golden hp-011
        assert dob_evidence(date(1958, 3, 11), date(1958, 3, 12)) is DobEvidence.MATCH

    def test_day_month_swap_is_a_match(self) -> None:  # golden hp-013
        assert dob_evidence(date(1963, 8, 30), date(1963, 8, 3)) is DobEvidence.MATCH
        assert dob_evidence(date(1990, 4, 7), date(1990, 7, 4)) is DobEvidence.MATCH

    def test_same_day_different_year_is_a_mismatch(self) -> None:  # golden hp-012
        assert dob_evidence(date(1985, 11, 2), date(1971, 11, 2)) is DobEvidence.MISMATCH

    def test_missing_side_is_unknown_never_mismatch(self) -> None:
        assert dob_evidence(None, date(1990, 1, 1)) is DobEvidence.UNKNOWN
        assert dob_evidence(date(1990, 1, 1), None) is DobEvidence.UNKNOWN
        assert dob_evidence(None, None) is DobEvidence.UNKNOWN
