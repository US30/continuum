"""Eval-only ground truth about identities and networks (CLAUDE.md I4).

Three small files, written next to the other eval-only truth files, that only eval/ may read:

  record_truth.csv    record_id, person_id
        Which real person every identity record belongs to (including records that opened no
        account), so an entity's members can be checked against ground truth.

  account_truth.csv   account_id, record_id, person_id
        Which identity record each account was originated under, and which real person that
        record belongs to. The evaluation of Metric 3 needs the *record* an account came from
        to say what an account-centric system would have linked (it groups by the identifiers
        on that record), and the *person* to check that a link the entity view made is real.

  network_truth.csv   person_id, group_id, group_type
        Planted households (benign) and fraud rings, so the fraud signature and the network
        links can be scored instead of merely counted.

tests/test_invariants.py fails the build if runtime code (anything under continuum/ except this
writer, the build entrypoint and the causal docs) mentions these files.
"""

from __future__ import annotations

import csv
from pathlib import Path

from continuum.foundation.collisions import PartyRecordDraft
from continuum.foundation.generate import SyntheticPerson


def write_eval_truth(
    persons: list[SyntheticPerson],
    drafts: list[PartyRecordDraft],
    account_truth_path: Path,
    network_truth_path: Path,
    record_truth_path: Path,
) -> tuple[int, int]:
    """Returns (accounts written, grouped persons written). Deterministic (I6)."""
    account_truth_path.parent.mkdir(parents=True, exist_ok=True)
    n_accounts = 0
    with account_truth_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["account_id", "record_id", "person_id"])
        for draft in drafts:
            for account_id in draft.account_ids:
                writer.writerow([account_id, draft.record_id, draft.person_id])
                n_accounts += 1

    with record_truth_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["record_id", "person_id"])
        writer.writerows((d.record_id, d.person_id) for d in drafts)

    n_grouped = 0
    with network_truth_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["person_id", "group_id", "group_type"])
        for person in persons:
            if person.group_id is not None:
                writer.writerow([person.person_id, person.group_id, person.group_type])
                n_grouped += 1
    return n_accounts, n_grouped
