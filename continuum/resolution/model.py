"""Splink dedupe model: per-attribute comparison levels for party_record clustering.

CLAUDE.md §8: "Splink model with per-attribute comparison levels (Jaro-Winkler on names,
exact-then-fuzzy on address, exact on identifiers)." CLAUDE.md §4: Splink 4, DuckDB backend
locally (Postgres in cloud).

Comparisons use Splink's built-in comparison templates, which ship with reasonable default
m/u probabilities for out-of-the-box use. That matters here specifically because this build
never runs an EM training pass in-session (CLAUDE.md §10 — no threshold in this file has
been tuned against real match rates yet; that is Phase 2 follow-up work once the pipeline
has actually executed once against the synthetic dataset and continuum.resolution has a
real precision/recall reading from eval.resolution_metrics to tune against).

Not executed this session (per the user's request — code only, nothing run yet): the exact
`splink.comparison_library` class names below (JaroWinklerAtThresholds, ExactMatch) match
the Splink 4 API as documented at time of writing, but Splink's public API has moved across
versions before. First real run against the pinned `splink>=4.0` in pyproject.toml is the
actual verification step — expect this file to need a naming fixup then, not a redesign.
"""

from __future__ import annotations

from typing import Any

import splink.comparison_library as cl
from splink import DuckDBAPI, Linker, SettingsCreator
from splink.internals.comparison_creator import ComparisonCreator

from continuum.resolution.blocking import BLOCKING_RULES

# Jaro-Winkler on names (CLAUDE.md §8), with term-frequency adjustment so a common name
# like "James Smith" needs more corroborating evidence than a rare one.
NAME_COMPARISON = cl.JaroWinklerAtThresholds(
    "full_name", score_threshold_or_thresholds=[0.9, 0.7]
).configure(term_frequency_adjustments=True)

# Exact-then-fuzzy on address (CLAUDE.md §8): an exact match on the raw address line is the
# top level, a close Jaro-Winkler match (catches "Street" vs "St" abbreviation drift from
# continuum.foundation.identity.address_variant) is the middle level, else "else".
ADDRESS_COMPARISON = cl.JaroWinklerAtThresholds(
    "address_line", score_threshold_or_thresholds=[0.95, 0.85]
)

# Exact on identifiers (CLAUDE.md §8) — national_id is either an exact match (after
# continuum.foundation.identity.id_variant's punctuation-only drift, which blocking already
# tolerates by not requiring an exact match to compare in the first place) or it isn't.
NATIONAL_ID_COMPARISON = cl.ExactMatch("national_id").configure(term_frequency_adjustments=True)

DATE_OF_BIRTH_COMPARISON = cl.ExactMatch("date_of_birth")
POSTAL_CODE_COMPARISON = cl.ExactMatch("postal_code")
PHONE_COMPARISON = cl.ExactMatch("phone")

COMPARISONS: list[ComparisonCreator | dict[str, Any]] = [
    NAME_COMPARISON,
    DATE_OF_BIRTH_COMPARISON,
    NATIONAL_ID_COMPARISON,
    ADDRESS_COMPARISON,
    POSTAL_CODE_COMPARISON,
    PHONE_COMPARISON,
]

SETTINGS = SettingsCreator(
    link_type="dedupe_only",
    unique_id_column_name="record_id",
    comparisons=COMPARISONS,
    blocking_rules_to_generate_predictions=BLOCKING_RULES,
    retain_intermediate_calculation_columns=True,
)


def build_linker(records: Any) -> Linker:
    """`records` is one row per party_record with columns record_id, full_name,
    date_of_birth, national_id, address_line, postal_code, phone — see
    continuum.resolution.run for how it is loaded from Postgres. Untyped (`Any`) because
    Splink accepts a pandas DataFrame or a DuckDB relation interchangeably here.
    """
    return Linker(records, SETTINGS, db_api=DuckDBAPI())
