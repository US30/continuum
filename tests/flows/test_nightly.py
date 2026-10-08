"""The nightly flow adds only ordering to the stage functions, so that is what is tested: the
stages run in CLAUDE.md §7's order, with the settings that gate the adjudicator, and a failing
stage stops the night. Stage bodies are stubbed; each has its own tests elsewhere."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from prefect.testing.utilities import prefect_test_harness

from continuum.config.settings import Settings
from continuum.flows import nightly


@pytest.fixture(scope="module", autouse=True)
def _prefect() -> Iterator[None]:
    with prefect_test_harness():
        yield


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    seen: list[tuple[str, Any]] = []
    settings = Settings(llm_enabled=False, screening_max_adjudications_per_run=250)
    monkeypatch.setattr(nightly, "get_settings", lambda: settings)
    monkeypatch.setattr(
        nightly.foundation_build, "build", lambda **kw: seen.append(("foundation.refresh", kw))
    )
    monkeypatch.setattr(
        nightly.resolution_run,
        "resolve",
        lambda: seen.append(("resolution.run", None)) or (9, 7, 2),
    )
    monkeypatch.setattr(nightly.screening_run, "run", lambda **kw: seen.append(("screening", kw)))
    monkeypatch.setattr(
        nightly.audit_sample, "fn", lambda: seen.append(("audit.sample", None)) or {"suspects": 0}
    )
    monkeypatch.setattr(
        nightly.train_scoring_model, "fn", lambda: seen.append(("scoring.train", None)) or "v-test"
    )
    return seen


def test_stages_run_in_the_documented_order(calls: list[tuple[str, Any]]) -> None:
    summary = nightly.nightly_batch(n_persons=50)
    assert [name for name, _ in calls] == [
        "foundation.refresh",
        "resolution.run",
        "screening",
        "audit.sample",
        "scoring.train",
    ]
    assert calls[0][1] == {"n_persons": 50, "seed": None}
    assert summary["resolution"] == {"records": 9, "clusters": 7, "merged": 2}
    assert summary["model_version"] == "v-test"


def test_screening_follows_the_llm_switch_and_the_spend_cap(calls: list[tuple[str, Any]]) -> None:
    nightly.nightly_batch(n_persons=50, full_screen=True)
    assert dict(calls)["screening"] == {
        "adjudicate_enabled": False,
        "full": True,
        "max_adjudications": 250,
    }


def test_a_failing_stage_stops_the_night(
    calls: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> None:
        raise RuntimeError("splink failed")

    monkeypatch.setattr(nightly.resolution_run, "resolve", broken)
    with pytest.raises(RuntimeError, match="splink failed"):
        nightly.nightly_batch(n_persons=50)
    assert [name for name, _ in calls] == ["foundation.refresh"]
