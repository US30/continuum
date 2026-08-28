"""Phase 6 — the compliance guard. Deterministic rules, then tone check.

CLAUDE.md I2: the guard is the single egress point. No other module may dispatch an
outbound message. See tests/test_invariants.py for the enforced import boundary.

Not yet implemented beyond the dispatch stub. mypy --strict applies (CLAUDE.md §10).
"""
