"""The regression gate must fail when any of the three metrics regresses (CLAUDE.md Phase 7
done-when: "CI fails on regression in any of the three metrics") and pass when nothing did.
Uses the committed eval/baselines.json as the reference."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from eval.metrics import BASELINES, GATES, check

BASELINE: dict[str, Any] = json.loads(BASELINES.read_text(encoding="utf-8"))


def report() -> dict[str, Any]:
    return copy.deepcopy(BASELINE["values"])


def set_path(tree: dict[str, Any], dotted: str, value: float) -> None:
    *parents, leaf = dotted.split(".")
    node = tree
    for part in parents:
        node = node[part]
    node[leaf] = value


def get_path(tree: dict[str, Any], dotted: str) -> float:
    node: Any = tree
    for part in dotted.split("."):
        node = node[part]
    return float(node)


def test_an_unchanged_report_passes() -> None:
    assert check(report(), BASELINE) == []


def test_the_baseline_itself_satisfies_every_absolute_requirement() -> None:
    values = BASELINE["values"]
    assert values["metric1_screening"]["recall_deterministic_layer"] == 1.0
    assert values["metric1_screening"]["planted_hits_false_cleared"] == 0
    assert values["metric2_uplift"]["ci_low"] > 0
    assert values["supporting"]["injection_attacks_succeeded"] == 0
    assert values["supporting"]["guard_golden_wrong"] == 0


@pytest.mark.parametrize("path,direction,kind,tol", GATES)
def test_every_gate_fails_when_its_metric_regresses(
    path: str, direction: str, kind: str, tol: float
) -> None:
    broken = report()
    base = get_path(broken, path)
    if kind == "abs_zero":
        set_path(broken, path, 0.0)
    else:
        margin = abs(base) * tol if kind == "rel" else tol
        set_path(broken, path, base - margin - 0.01 if direction == "min" else base + margin + 0.01)
    failures = check(broken, BASELINE)
    assert any(path in f for f in failures), (path, failures)


def test_a_metric_within_tolerance_does_not_fail() -> None:
    ok = report()
    set_path(
        ok,
        "metric3_network.validated_share",
        get_path(ok, "metric3_network.validated_share") - 0.01,
    )
    assert check(ok, BASELINE) == []


def test_an_improvement_never_fails() -> None:
    better = report()
    set_path(better, "metric2_uplift.incremental_recovery_oracle", 1e9)
    set_path(better, "metric3_network.validated_share", 0.99)
    assert check(better, BASELINE) == []


def test_the_three_headline_metrics_are_all_gated() -> None:
    paths = {p.split(".")[0] for p, *_ in GATES}
    assert {"metric1_screening", "metric2_uplift", "metric3_network"} <= paths


def test_running_the_gate_on_a_different_sized_dataset_is_refused_not_silently_compared() -> None:
    other = report()
    other["config"]["n_accounts"] = int(BASELINE["config"]["n_accounts"] * 2.5)
    failures = check(other, BASELINE)
    assert len(failures) == 1 and "dataset mismatch" in failures[0]
