"""Tests for the OpenSanctions targets.simple.csv parser.

Uses a synthetic fixture (tests/fixtures/sanctions_sample.csv) rather than a real
OpenSanctions export — CI never needs the real download (see plan's I8 discussion).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from continuum.foundation.sanctions import parse_targets_simple_csv, snapshot_id_for

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sanctions_sample.csv"


class TestParseTargetsSimpleCsv:
    def test_parses_all_rows(self) -> None:
        listings = parse_targets_simple_csv(FIXTURE)
        assert len(listings) == 3
        assert {listing.listing_id for listing in listings} == {
            "synth-0001",
            "synth-0002",
            "synth-0003",
        }

    def test_explodes_semicolon_joined_aliases(self) -> None:
        listings = {listing.listing_id: listing for listing in parse_targets_simple_csv(FIXTURE)}
        assert listings["synth-0001"].aliases == ["A. Vantessen", "Aldric Van Tessen"]
        assert listings["synth-0002"].aliases == ["Mari Quintrada"]

    def test_parses_birth_date(self) -> None:
        listings = {listing.listing_id: listing for listing in parse_targets_simple_csv(FIXTURE)}
        assert listings["synth-0001"].birth_date == date(1958, 3, 12)

    def test_missing_required_column_raises(self, tmp_path: Path) -> None:
        bad_csv = tmp_path / "bad.csv"
        bad_csv.write_text("id,schema,name\nx,Person,Someone\n")
        with pytest.raises(ValueError, match="missing columns"):
            parse_targets_simple_csv(bad_csv)

    def test_blank_birth_date_is_none(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "one.csv"
        csv_path.write_text(
            "id,schema,name,aliases,birth_date,countries,topics\nx,Person,No Birth Date,,,,\n"
        )
        listings = parse_targets_simple_csv(csv_path)
        assert listings[0].birth_date is None
        assert listings[0].aliases == []


class TestSnapshotId:
    """CLAUDE.md I6: reloading the same listing must not invalidate existing decisions."""

    def test_same_file_gives_the_same_snapshot_id(self) -> None:
        assert snapshot_id_for(FIXTURE) == snapshot_id_for(FIXTURE)

    def test_changed_file_gives_a_new_snapshot_id(self, tmp_path: Path) -> None:
        changed = tmp_path / "changed.csv"
        changed.write_text(FIXTURE.read_text() + "synth-9999,Person,New Listing,,,,\n")
        assert snapshot_id_for(changed) != snapshot_id_for(FIXTURE)
