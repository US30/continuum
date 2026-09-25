"""Blocking rules for Splink candidate-pair generation.

CLAUDE.md I5: "Screening is recall-first... never tune it toward precision." The same
principle governs blocking here: a rule is only safe to add if two party_records that are
actually the same person are still virtually guaranteed to share at least one blocking key.
Splink ORs the rules in this list — a pair is compared if it satisfies *any* one of them —
so each rule only needs to catch the cases the others miss, not be safe on its own.
"""

from __future__ import annotations

from typing import Any

from splink import block_on
from splink.internals.blocking_rule_creator import BlockingRuleCreator

BLOCKING_RULES: list[BlockingRuleCreator | dict[str, Any]] = [
    # Two records citing the same national id are near-certainly the same person, even if
    # every other field (name, address) has drifted.
    block_on("national_id"),
    # Same address + same date of birth, independent of name — catches nicknames,
    # transliteration drift, and transposed name order (CLAUDE.md §8's hard-name-pair set).
    block_on("postal_code", "date_of_birth"),
    # Same phone number, name and address allowed to differ entirely — a household member
    # or a person who moved but kept their number.
    block_on("phone"),
    # First four characters of the name plus date of birth — a looser net for records
    # where address/phone/national_id all disagree or are missing outright.
    block_on("substr(full_name, 1, 4)", "date_of_birth"),
]
