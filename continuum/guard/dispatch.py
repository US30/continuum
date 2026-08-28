"""The single egress point for outbound customer messages.

CLAUDE.md I2: "The compliance guard is the single egress point. No code path may dispatch
an outbound message without a guard verdict." Concretely: `send()` below is the only
function anywhere in the codebase that is allowed to hand a message to the dispatch
worker (Phase 6, continuum/workers/). tests/test_invariants.py asserts that nothing
outside continuum/guard/ imports this module.

Not yet implemented — Phase 6 wires this to the Arq dispatch worker after both the
deterministic rule pass and the tone check pass.
"""

from __future__ import annotations


class GuardVerdictRequiredError(RuntimeError):
    """Raised if send() is ever reached without a prior guard verdict."""


def send(*_args: object, **_kwargs: object) -> None:
    """Placeholder egress point. Phase 6 replaces this with the real dispatch call,
    gated on a GuardVerdict produced by continuum.guard's deterministic rules + tone check.
    """
    raise NotImplementedError("dispatch.send is not implemented until Phase 6")
