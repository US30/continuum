"""Phase 4 — self-cure model, response model, uplift, calibration.

Entrypoints: `make train` (continuum.scoring.train) and `make uplift-metrics`
(eval.uplift_metrics, Metric 2). CLAUDE.md I1: no LLM anywhere in this package. Nothing here
may read the eval-only oracle truth file (I4); tests/test_invariants.py enforces it.
"""
