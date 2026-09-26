"""One report for everything the project is judged on, and the regression gate CI runs.

CLAUDE.md §9 (the three metrics) and Phase 7 done-when: "CI fails on regression in any of the
three metrics". `collect()` computes:

  Metric 1  screening      recall of the deterministic layer on the labelled hard pairs (must stay
                           1.0: recall-first, I5), the share of pairs reaching the model, and the
                           planted-hit false-clear count from the audit census. Precision after the
                           LLM adjudicator is a stage-2 number that needs a live model, so it is
                           reported by `make screen-metrics-llm` and is not a CI gate.
  Metric 2  uplift         incremental recovery of uplift ranking vs the rules baseline, in
                           currency (oracle estimator on the time-held-out test episodes).
  Metric 3  network        share of collections cases where the entity view beat the account view
                           (truth-validated).
  Supporting               resolution F1, guard golden set, injection suite, audit suspect rate.

`check()` compares a report to eval/baselines.json (committed). A regression is a change past
the tolerance recorded next to each baseline value. The baseline is refreshed only by the
explicit `make metrics-baseline` — never automatically — so an improvement or a regression is
always a reviewed diff. Tolerances exist because numbers computed on another machine can differ
in the last digits, not to excuse real decay.

Determinism: the CI config (n_persons, seed) is stored in the baseline; the pipeline is seeded
end to end (data, Splink, LightGBM), so the same config reproduces the same report.

Run via `make metrics` (report), `make metrics-check` (gate), `make metrics-baseline`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from continuum.config.settings import get_settings
from continuum.db.models.account import Account
from continuum.db.session import session_scope
from eval import audit, guard_golden, injection_suite, network_value, uplift_metrics
from eval.resolution_metrics import compute_resolution_metrics
from eval.screening_metrics import compute_screening_metrics, load_golden_set

BASELINES = Path(__file__).parent / "baselines.json"
REPORT = Path("data") / "metrics_report.json"


def collect() -> dict[str, Any]:
    settings = get_settings()

    with session_scope() as session:
        n_accounts = int(session.scalar(select(func.count()).select_from(Account)) or 0)

    pairs = load_golden_set()
    screening, missed = compute_screening_metrics(pairs)

    with session_scope() as session:
        resolution = compute_resolution_metrics(session)
        audit_report = audit.run_audit(session, settings.data_as_of_date)

    metric2 = uplift_metrics.compute()
    truth = network_value.load_truth()
    with session_scope() as session:
        cases = network_value.load_cases(session, truth)
    metric3 = network_value.compute_metric3(cases, truth)

    injection = injection_suite.run_suite()
    guard = guard_golden.run()

    return {
        "config": {
            "data_seed": settings.data_seed,
            "as_of": settings.data_as_of_date.isoformat(),
            "n_accounts": n_accounts,
        },
        "metric1_screening": {
            "labelled_pairs": screening.total,
            "recall_deterministic_layer": screening.recall,
            "missed_true_matches": [p.case_id for p in missed],
            "proportion_reaching_model": screening.proportion_reaching_model,
            "planted_hits_total": audit_report.planted_total,
            "planted_hits_false_cleared": audit_report.planted_cleared_by_production,
        },
        "metric2_uplift": {
            "n_test_episodes": metric2.n_test,
            "incremental_recovery_oracle": metric2.metric2_oracle,
            "ci_low": metric2.metric2_oracle_ci[0],
            "ci_high": metric2.metric2_oracle_ci[1],
            "vs_rules_top_k": metric2.metric2_vs_rules_top_k,
            "spearman_tau": metric2.spearman_tau,
        },
        "metric3_network": {
            "n_cases": metric3.n_cases,
            "validated_share": metric3.validated_share,
            "raw_share": metric3.raw_share,
            "ring_cases": metric3.ring_cases,
            "ring_recall": metric3.ring_cases_flagged / max(metric3.ring_cases, 1),
            "non_ring_flag_rate": metric3.non_ring_cases_flagged / max(metric3.non_ring_cases, 1),
        },
        "supporting": {
            "resolution_precision": resolution.precision,
            "resolution_recall": resolution.recall,
            "resolution_f1": resolution.f1,
            "audit_suspect_rate": audit_report.suspect_rate,
            "injection_attacks": len(injection),
            "injection_attacks_succeeded": sum(1 for o in injection if not o.defended),
            "guard_golden_cases": len(guard),
            "guard_golden_wrong": sum(1 for c in guard if not c.ok),
        },
        "_dataclasses": {"audit": asdict(audit_report)},
    }


# (path in report, direction, tolerance kind, tolerance): direction "min" = value must not
# fall below baseline - tol; "max" = must not exceed baseline + tol; "eq" = exact.
GATES: list[tuple[str, str, str, float]] = [
    ("metric1_screening.recall_deterministic_layer", "min", "abs", 0.0),
    ("metric1_screening.proportion_reaching_model", "max", "abs", 0.05),
    ("metric1_screening.planted_hits_false_cleared", "max", "abs", 0.0),
    ("metric2_uplift.incremental_recovery_oracle", "min", "rel", 0.20),
    ("metric2_uplift.ci_low", "min", "abs_zero", 0.0),
    ("metric2_uplift.spearman_tau", "min", "abs", 0.10),
    ("metric3_network.validated_share", "min", "abs", 0.02),
    ("metric3_network.ring_recall", "min", "abs", 0.10),
    ("supporting.resolution_f1", "min", "abs", 0.02),
    ("supporting.injection_attacks_succeeded", "max", "abs", 0.0),
    ("supporting.guard_golden_wrong", "max", "abs", 0.0),
]


def _get(tree: dict[str, Any], dotted: str) -> float:
    node: Any = tree
    for part in dotted.split("."):
        node = node[part]
    return float(node)


def check(report: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Human-readable failures; empty means the gate passes."""
    failures: list[str] = []
    base_accounts = baseline.get("config", {}).get("n_accounts")
    now_accounts = report.get("config", {}).get("n_accounts")
    if base_accounts and now_accounts and abs(now_accounts - base_accounts) > 0.01 * base_accounts:
        return [
            f"dataset mismatch: {now_accounts} accounts vs the baseline's {base_accounts}. "
            "The gate is only meaningful on the canonical dataset: run `make ci-metrics`."
        ]
    for path, direction, kind, tol in GATES:
        value = _get(report, path)
        if kind == "abs_zero":  # an absolute floor of zero, independent of the baseline
            if value <= 0:
                failures.append(f"{path} = {value:.4f}, must be > 0")
            continue
        base = _get(baseline["values"], path)
        margin = abs(base) * tol if kind == "rel" else tol
        if direction == "min" and value < base - margin - 1e-9:
            failures.append(f"{path} regressed: {value:.4f} < baseline {base:.4f} - {margin:.4f}")
        if direction == "max" and value > base + margin + 1e-9:
            failures.append(f"{path} regressed: {value:.4f} > baseline {base:.4f} + {margin:.4f}")
    return failures


def _print(report: dict[str, Any]) -> None:
    m1, m2 = report["metric1_screening"], report["metric2_uplift"]
    m3, sup = report["metric3_network"], report["supporting"]
    lines = [
        "",
        "=== CONTINUUM metrics report ===",
        (
            f"Metric 1  recall (deterministic layer) {m1['recall_deterministic_layer']:.4f}"
            f"   reaching model {m1['proportion_reaching_model']:.3f}"
            f"   planted hits false-cleared "
            f"{m1['planted_hits_false_cleared']}/{m1['planted_hits_total']}"
        ),
        (
            f"Metric 2  incremental recovery vs rules baseline (oracle) "
            f"${m2['incremental_recovery_oracle']:,.0f}"
            f"   95% CI [{m2['ci_low']:,.0f}, {m2['ci_high']:,.0f}]"
            f"   Spearman {m2['spearman_tau']:.3f}"
        ),
        (
            f"Metric 3  entity view beat account view: "
            f"{100 * m3['validated_share']:.1f}% validated ({100 * m3['raw_share']:.1f}% raw) "
            f"of {m3['n_cases']} cases; ring recall {m3['ring_recall']:.2f}, "
            f"non-ring flag rate {m3['non_ring_flag_rate']:.3f}"
        ),
        (
            f"supporting  resolution F1 {sup['resolution_f1']:.4f}"
            f"   injection {sup['injection_attacks'] - sup['injection_attacks_succeeded']}"
            f"/{sup['injection_attacks']} defended"
            f"   guard golden {sup['guard_golden_cases'] - sup['guard_golden_wrong']}"
            f"/{sup['guard_golden_cases']}"
            f"   audit suspect rate {100 * sup['audit_suspect_rate']:.1f}%"
        ),
        "",
    ]
    print("\n".join(lines))


def main(argv: list[str]) -> int:
    report = collect()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    _print(report)

    if "--write-baseline" in argv:
        values = {k: v for k, v in report.items() if not k.startswith("_")}
        BASELINES.write_text(
            json.dumps({"config": report["config"], "values": values}, indent=2), encoding="utf-8"
        )
        print(f"baseline written: {BASELINES}")
        return 0

    if "--check" in argv:
        baseline = json.loads(BASELINES.read_text(encoding="utf-8"))
        failures = check(report, baseline)
        if failures:
            print("METRIC REGRESSION — CI gate FAILED:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("metric gate passed: no regression vs eval/baselines.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
