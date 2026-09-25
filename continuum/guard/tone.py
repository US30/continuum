"""LLM tone check — the second, probabilistic layer of the guard.

CLAUDE.md §7: "deterministic rules FIRST ... then tone check". Properties this module
guarantees, whatever the model says:

  * It runs only after the deterministic rules; the engine never calls it for a message that
    is already BLOCKed, so an attacker cannot use it to launder a rule violation.
  * It can only *downgrade*: an unacceptable message becomes ESCALATE. It can never turn a
    BLOCK or ESCALATE into a PASS.
  * If it cannot run (model outage, LLM disabled, malformed output) the result is
    `checked=False`, which the engine treats as ESCALATE — fail closed.
  * The message is fenced as untrusted data (continuum.llm.untrusted) and the reported issues
    are sanitised and truncated before they enter the verdict.

Residual risk, stated plainly: this layer is an LLM and can be fooled. The hard rules and the
signed verdict do not depend on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from continuum.guard.models import ToneResult
from continuum.llm.client import complete_structured_with_usage
from continuum.llm.untrusted import sanitize, wrap

PROMPT_DIR = Path(__file__).parent / "prompts"
PROMPT_VERSION = "tone_v1"
_SYSTEM = (PROMPT_DIR / f"{PROMPT_VERSION}_system.txt").read_text(encoding="utf-8")
_USER = (PROMPT_DIR / f"{PROMPT_VERSION}_user.txt").read_text(encoding="utf-8")

MAX_ISSUES = 5
MAX_ISSUE_LEN = 100
MAX_MESSAGE_CHARS_FOR_MODEL = 1200


class ToneVerdict(BaseModel):
    acceptable: bool
    issues: list[str] = Field(default_factory=list)


class ToneChecker(Protocol):
    def check(self, message: str, channel: str) -> ToneResult: ...


class LLMToneChecker:
    """The real checker, routed through continuum.llm (CLAUDE.md I7)."""

    def check(self, message: str, channel: str) -> ToneResult:
        user = _USER.format(
            channel=sanitize(channel, 20),
            message=wrap("message", message, MAX_MESSAGE_CHARS_FOR_MODEL),
        )
        try:
            result = complete_structured_with_usage("guard_tone", _SYSTEM, user, ToneVerdict)
        except Exception as exc:
            return unavailable(f"{type(exc).__name__}")
        usage = result.usage
        issues = [sanitize(i, MAX_ISSUE_LEN) for i in result.parsed.issues[:MAX_ISSUES]]
        return ToneResult(
            checked=True,
            acceptable=result.parsed.acceptable,
            issues=issues,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
        )


class DisabledToneChecker:
    """Used when Settings.llm_enabled is False: no call, no cost, and every message that
    reaches it is escalated to a human (fail closed)."""

    def check(self, message: str, channel: str) -> ToneResult:
        return unavailable("LLM disabled")


def unavailable(why: str) -> ToneResult:
    return ToneResult(checked=False, acceptable=False, reason=f"tone check unavailable: {why}")
