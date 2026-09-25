"""Eval-only truth file for the causal outcome model.

Writes the oracle per-episode quantities the evaluation harness needs to score uplift
exactly: P(cure | no contact), P(cure | contact), the realised treatment and outcome, and
the overdue amount a cure recovers. Mirrors ground_truth_link / sanctions_planted.csv:
CLAUDE.md I4 — this file is invisible to runtime code. Only eval/ may read it, and
tests/test_invariants.py fails the build if anything under continuum/ other than this
writer mentions it.
"""

from __future__ import annotations

import csv
from pathlib import Path

from continuum.foundation.generate import SyntheticPerson

HEADER = [
    "account_id",
    "decision_date",
    "treated",
    "cured_60",
    "overdue_amount",
    "p_cure_control",
    "p_cure_treated",
]


def write_true_uplift(persons: list[SyntheticPerson], path: Path) -> int:
    """Returns the number of episodes written. Deterministic given `persons` (I6)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        for person in persons:
            for account in person.accounts:
                ep = account.episode
                if ep is None:
                    continue
                writer.writerow(
                    [
                        account.account_id,
                        ep.decision_date.isoformat(),
                        int(ep.treated),
                        int(ep.cured_60),
                        ep.overdue_amount,
                        f"{ep.p_cure_control:.6f}",
                        f"{ep.p_cure_treated:.6f}",
                    ]
                )
                n += 1
    return n
