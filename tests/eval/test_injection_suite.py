"""CI gate for CLAUDE.md Phase 6: no injection in the suite succeeds — and the suite itself is
proven able to fail (a suite that cannot fail proves nothing)."""

from __future__ import annotations

import pytest

from continuum.graph.nodes import case_assembly
from continuum.guard import rules, signing
from eval import injection_suite
from eval.injection_suite import run_suite


def test_no_injection_in_the_suite_succeeds() -> None:
    failures = [(o.attack.id, o.succeeded_how) for o in run_suite() if not o.defended]
    assert failures == []


def test_suite_is_substantial_and_covers_every_surface() -> None:
    attacks = injection_suite.build_attacks()
    assert len(attacks) >= 45
    assert {a.surface for a in attacks} == {"NAME", "MODEL_OUT", "FORGERY", "FENCE"}
    assert len({a.id for a in attacks}) == len(attacks)


class TestTheSuiteCanFail:
    """Weaken one defence at a time; the matching attacks must start succeeding."""

    def test_without_name_validation_name_attacks_succeed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(case_assembly, "sanitize_person_name", lambda v, **kw: str(v))
        failed = {o.attack.surface for o in run_suite() if not o.defended}
        assert "NAME" in failed

    def test_without_the_deterministic_rules_model_output_attacks_succeed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rules, "ALL_RULES", (rules.screening_resolved,))
        monkeypatch.setattr("continuum.guard.engine.ALL_RULES", (rules.screening_resolved,))
        failed = {o.attack.surface for o in run_suite() if not o.defended}
        assert "MODEL_OUT" in failed

    def test_without_signature_verification_forgeries_succeed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(signing, "verify", lambda verdict, key: True)
        failed = {o.attack.surface for o in run_suite() if not o.defended}
        assert "FORGERY" in failed
