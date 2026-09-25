"""Everything a node needs from the outside world, injected once when the graph is built.

Nodes never construct sessions, model clients or scorers themselves. That keeps state free of
live objects (it is checkpointed), lets tests substitute a fake LLM and an in-memory scorer,
and keeps I7 honest: the only route to a model is `deps.llm`, whose real implementation is
continuum.llm.client.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy.orm import Session

from continuum.config.models import LogicalNode
from continuum.config.settings import Settings
from continuum.llm.client import LLMResult, complete_structured_with_usage
from continuum.scoring.runtime import AccountScore


class LLM(Protocol):
    def complete[T: BaseModel](
        self, node: LogicalNode, system_prompt: str, user_prompt: str, response_model: type[T]
    ) -> LLMResult[T]: ...


class GatewayLLM:
    """The real thing: routed, retried and circuit-broken by continuum.llm.client."""

    def complete[T: BaseModel](
        self, node: LogicalNode, system_prompt: str, user_prompt: str, response_model: type[T]
    ) -> LLMResult[T]:
        return complete_structured_with_usage(node, system_prompt, user_prompt, response_model)


class LLMDisabledError(RuntimeError):
    """Raised by DisabledLLM; nodes treat it like any model outage and fall back."""


class DisabledLLM:
    """Settings.llm_enabled = False: no network, no retries, no cost."""

    def complete[T: BaseModel](
        self, node: LogicalNode, system_prompt: str, user_prompt: str, response_model: type[T]
    ) -> LLMResult[T]:
        raise LLMDisabledError(f"LLM disabled (node {node})")


class ScoreProvider(Protocol):
    def score(self, account_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, AccountScore]: ...


@dataclass
class GraphDeps:
    session_scope: Callable[[], AbstractContextManager[Session]]
    scorer: ScoreProvider
    llm: LLM
    settings: Settings
