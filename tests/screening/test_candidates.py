"""Pure tests for ScreeningCandidate's derived fields. No DB."""

from __future__ import annotations

import json
from datetime import date

from continuum.screening.candidates import ScreeningCandidate


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
        assert evidence["dob_match"] is True
        assert evidence["name_similarity"] == 0.97

    def test_is_json_serialisable(self) -> None:
        json.dumps(_candidate().as_evidence())
