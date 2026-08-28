"""Phase 3 — candidate generation, band logic, LLM adjudicator node.

Entrypoint: continuum.screening.run (`make screen`). CLAUDE.md I1: everything in this
package up to and including band logic is deterministic — the only model call anywhere
here is continuum.screening.adjudicate, gated entirely behind continuum.screening.bands.
"""
