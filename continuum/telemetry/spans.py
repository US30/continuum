"""One OpenTelemetry span per decisioning-graph node, with LLM token count and cost.

CLAUDE.md Phase 5 done-when: "every node emits an OTel span with token count and cost", using
the OTel GenAI semantic-convention attribute names where one exists (`gen_ai.*`) and a
`continuum.*` namespace for the rest. Non-LLM nodes emit the same span with zero tokens, so a
trace always shows all seven nodes and where the money went.

Provider setup is explicit (`configure_tracing`) rather than global: tests inject an in-memory
exporter, the batch runner can add a console exporter, and cloud sets an OTLP endpoint
(Settings.otel_exporter_otlp_endpoint) -> Cloud Trace. Unconfigured, spans are no-ops.
PII: the entity id is hashed before it is attached (CLAUDE.md §10).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.trace import Span, Tracer

from continuum.telemetry.logging import get_correlation_id, hash_pii

TRACER_NAME = "continuum.graph"

_provider: TracerProvider | None = None


def configure_tracing(processors: Sequence[SpanProcessor]) -> TracerProvider:
    """Replaces the module's tracer provider. Returns it so callers can flush/shut down."""
    global _provider
    provider = TracerProvider()
    for processor in processors:
        provider.add_span_processor(processor)
    _provider = provider
    return provider


def _tracer() -> Tracer:
    if _provider is not None:
        return _provider.get_tracer(TRACER_NAME)
    from opentelemetry import trace

    return trace.get_tracer(TRACER_NAME)


@dataclass
class NodeUsage:
    """Accumulates LLM usage inside one node (a node may call the model more than once)."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""

    def record(
        self, model: str, input_tokens: int, output_tokens: int, cost_usd: float, calls: int = 1
    ) -> None:
        self.model = model or self.model
        self.calls += calls
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd += cost_usd


@contextmanager
def node_span(node: str, entity_id: uuid.UUID) -> Iterator[NodeUsage]:
    """Wrap a node body: `with node_span("draft", entity_id) as usage: ... usage.record(...)`."""
    usage = NodeUsage()
    with _tracer().start_as_current_span(f"continuum.node.{node}") as span:
        span.set_attribute("continuum.node", node)
        span.set_attribute("continuum.entity_hash", hash_pii(str(entity_id)))
        correlation_id = get_correlation_id()
        if correlation_id:
            span.set_attribute("continuum.correlation_id", correlation_id)
        try:
            yield usage
        finally:
            _finish(span, usage)


def _finish(span: Span, usage: NodeUsage) -> None:
    span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
    span.set_attribute("continuum.llm.calls", usage.calls)
    span.set_attribute("continuum.llm.cost_usd", round(usage.cost_usd, 8))
    if usage.model:
        span.set_attribute("gen_ai.request.model", usage.model)
