"""Thin LiteLLM gateway client: routing, structured-output parsing, retries, a circuit
breaker per logical node.

CLAUDE.md I7: "All model calls go through the LiteLLM gateway. No provider SDK imported
anywhere outside continuum/llm/." This module — and nothing else — imports `litellm`
(enforced by tests/test_invariants.py's provider-SDK check, since litellm itself is the
approved gateway, not a provider SDK). CLAUDE.md I2: the *deterministic* screening bands
(continuum.screening.bands) decide whether a candidate reaches this module at all — nothing
here can widen that gate.

mypy --strict applies to this package (CLAUDE.md §10). Flagged for the first real run,
same spirit as the Splink caveat in continuum/resolution/model.py: `litellm.completion`'s
return type is a loosely-typed `ModelResponse` without full inline stubs, so
`response.choices[0].message.content` below may need a `# type: ignore[...]` once mypy
actually runs against the installed litellm version — not a design problem, a stub gap.
"""

from __future__ import annotations

import json
import time
from typing import TypeVar

import litellm
from pydantic import BaseModel, ValidationError

from continuum.config.models import LogicalNode, model_for
from continuum.config.settings import get_settings
from continuum.telemetry.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

# Not tuned (CLAUDE.md §10) — a conservative starting point pending real latency/error-rate
# data from a live LiteLLM gateway.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0


class CircuitOpenError(RuntimeError):
    """Raised when a node's circuit breaker has tripped and is refusing calls."""


class CircuitBreaker:
    """Trips after `failure_threshold` consecutive failures and refuses further calls for
    `cooldown_seconds`; the first call after cooldown is let through as a half-open trial.
    Malformed-JSON / schema-validation failures do not count toward the threshold — those
    are a prompt problem, not a gateway-health problem, and retrying the same broken
    response format via a hard failure state would only mask the difference from a real
    outage.
    """

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: float = 60.0) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def before_call(self) -> None:
        if self._opened_at is None:
            return
        if time.monotonic() - self._opened_at < self.cooldown_seconds:
            raise CircuitOpenError("circuit breaker open — refusing LLM call")

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._opened_at = time.monotonic()


_circuit_breakers: dict[str, CircuitBreaker] = {}


def _circuit_breaker_for(node: LogicalNode) -> CircuitBreaker:
    return _circuit_breakers.setdefault(node, CircuitBreaker())


def complete_structured(
    node: LogicalNode,
    system_prompt: str,
    user_prompt: str,
    response_model: type[T],
    *,
    temperature: float = 0.0,
) -> T:
    """Calls the model routed to `node` (continuum.config.models.model_for) and parses its
    response as JSON into `response_model`. Retries on malformed JSON and on transport/
    provider failures; provider failures also count toward that node's circuit breaker.
    """
    settings = get_settings()
    model_id = model_for(node)
    breaker = _circuit_breaker_for(node)

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        breaker.before_call()
        try:
            response = litellm.completion(
                model=model_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                api_base=settings.litellm_base_url,
                api_key=settings.litellm_api_key or None,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            parsed = response_model.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            logger.warning("llm.malformed_response", node=node, attempt=attempt, error=str(exc))
        except Exception as exc:
            last_error = exc
            breaker.record_failure()
            logger.warning("llm.call_failed", node=node, attempt=attempt, error=str(exc))
        else:
            breaker.record_success()
            return parsed

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    assert last_error is not None
    raise last_error
