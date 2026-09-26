from __future__ import annotations

from eval.guard_golden import run


def test_every_golden_case_gets_its_expected_verdict() -> None:
    wrong = [(c.case_id, c.expected, c.actual) for c in run() if not c.ok]
    assert wrong == []


def test_golden_set_covers_every_verdict_and_is_substantial() -> None:
    cases = run()
    assert len(cases) >= 30
    assert {c.expected for c in cases} == {"pass", "block", "escalate"}
