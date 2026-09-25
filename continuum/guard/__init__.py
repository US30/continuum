"""Phase 5/6 — the compliance guard. Deterministic rules first (Phase 5), then tone check
and dispatch (Phase 6).

CLAUDE.md I2: the guard is the single egress point. No other module sends. Every module in
this package is mypy --strict and free of model calls until the Phase 6 tone check, which runs
only after the deterministic rules have passed. See tests/test_invariants.py for the enforced
import boundary around continuum.guard.dispatch.
"""
