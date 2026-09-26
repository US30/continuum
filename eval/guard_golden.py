"""Guard golden set: labelled messages/situations with the verdict the deterministic guard
MUST produce (`eval/golden/guard_messages.csv`). Accuracy must be 100% — these are rules, not
statistics; any miss is a regression. The tone model is stubbed as approving everything, which
is the worst case for the guard: whatever the rules decide, they decide without help.

Run via `make guard-golden` (`python -m eval.guard_golden`).
"""

from __future__ import annotations

import csv
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from continuum.config.settings import get_settings
from continuum.guard.engine import evaluate
from continuum.guard.models import GuardInput, ToneResult
from continuum.screening.status import ScreeningState

GOLDEN = Path(__file__).parent / "golden" / "guard_messages.csv"


class _ApprovingTone:
    def check(self, message: str, channel: str) -> ToneResult:
        return ToneResult(checked=True, acceptable=True)


@dataclass(frozen=True)
class GuardCase:
    case_id: str
    expected: str
    actual: str
    note: str

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


def run() -> list[GuardCase]:
    settings = get_settings()
    out: list[GuardCase] = []
    with GOLDEN.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            channel = None if row["channel"] == "none" else row["channel"]
            inp = GuardInput(
                entity_id=uuid.uuid4(),
                screening=ScreeningState(row["screening"]),
                total_overdue=float(row["total_overdue"]),
                channel=channel,
                has_phone=True,
                has_email=True,
                send_at=datetime(2026, 1, 2, int(row["hour"]), 0),
                recent_contacts_7d=int(row["recent_contacts_7d"]),
                message_body=row["body"],
            )
            verdict = evaluate(inp, settings, _ApprovingTone())
            out.append(
                GuardCase(
                    row["case_id"], row["expected_verdict"], verdict.verdict.value, row["note"]
                )
            )
    return out


def main() -> int:
    cases = run()
    wrong = [c for c in cases if not c.ok]
    print(f"guard golden set: {len(cases) - len(wrong)}/{len(cases)} correct")
    for c in wrong:
        print(f"  {c.case_id}: expected {c.expected}, got {c.actual}  ({c.note})")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
