from __future__ import annotations

import csv
from pathlib import Path

from continuum.foundation.collisions import build_party_records
from continuum.foundation.generate import generate_population
from continuum.foundation.sanctions import parse_targets_simple_csv
from continuum.foundation.synthetic_sanctions import write_synthetic_sanctions


def _write(tmp_path: Path, seed: int = 5) -> tuple[Path, Path, int]:
    drafts, _ = build_party_records(generate_population(400, seed), seed)
    csv_path, planted_path = tmp_path / "t.csv", tmp_path / "p.csv"
    return csv_path, planted_path, write_synthetic_sanctions(drafts, seed, csv_path, planted_path)


def test_output_parses_with_the_real_ingest_parser(tmp_path: Path) -> None:
    csv_path, _, n = _write(tmp_path)
    assert len(parse_targets_simple_csv(csv_path)) == n


def test_deterministic(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a_csv, a_planted, _ = _write(tmp_path / "a")
    b_csv, b_planted, _ = _write(tmp_path / "b")
    assert a_csv.read_text() == b_csv.read_text()
    assert a_planted.read_text() == b_planted.read_text()


def test_planted_truth_maps_to_existing_listings_and_not_by_id_prefix(tmp_path: Path) -> None:
    csv_path, planted_path, _ = _write(tmp_path)
    listing_ids = {row["id"] for row in csv.DictReader(csv_path.open())}
    planted = list(csv.DictReader(planted_path.open()))
    assert planted
    assert all(row["listing_id"] in listing_ids for row in planted)
    assert all(lid.startswith("synth-") and "planted" not in lid for lid in listing_ids)
