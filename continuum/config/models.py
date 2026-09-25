"""Logical-node -> model-id routing table.

CLAUDE.md I7: "Model choice is configuration... a swap is a config change." Every LLM call
site in continuum/graph and continuum/screening looks up its model by logical node name
through continuum.llm, which reads this table — never a hardcoded model id at the call site.

CLAUDE.md §4 planned DeepSeek-V4 for prioritisation/strategy, Llama 3.2 8B for screening
adjudication and the guard tone check, and Llama 3 8B 4-bit for drafting. By decision (user,
Sept 2026) screening_adjudicator, prioritise, strategy and draft all run on the DeepSeek API
(`deepseek-flash`, V4.1-Flash) instead of self-hosted Ollama; guard_tone is still the Phase 6
placeholder. Synthetic data only (I8) makes the PII-residency reason for self-hosting moot for
now — revisit before any real data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

LogicalNode = Literal[
    "screening_adjudicator",
    "prioritise",
    "strategy",
    "draft",
    "guard_tone",
]

# LiteLLM model identifiers, served locally via Ollama/vLLM (CLAUDE.md §4: PII residency).
MODEL_ROUTING: Final[dict[LogicalNode, str]] = {
    "screening_adjudicator": "deepseek/deepseek-flash",
    "prioritise": "deepseek/deepseek-flash",
    "strategy": "deepseek/deepseek-flash",
    "draft": "deepseek/deepseek-flash",
    "guard_tone": "ollama/llama3.2:8b",
}


def model_for(node: LogicalNode) -> str:
    """Resolve a logical node name to its configured model id."""
    return MODEL_ROUTING[node]


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens."""

    input_cache_miss: float
    input_cache_hit: float
    output: float


# LiteLLM has no price entry for these ids, so cost accounting (CLAUDE.md Phase 5 "token
# count and cost" on every span) uses this table. Peak-hour DeepSeek rates (off-peak is half),
# i.e. a conservative upper bound. Source: DeepSeek API pricing page, Sept 2026.
MODEL_PRICES: Final[dict[str, ModelPrice]] = {
    "deepseek/deepseek-flash": ModelPrice(0.30, 0.006, 1.20),
    "deepseek/deepseek-v4-pro": ModelPrice(1.32, 0.044, 3.96),
}


def cost_usd(
    model_id: str, prompt_tokens: int, cache_hit_tokens: int, completion_tokens: int
) -> float:
    """0.0 for models with no known price (local models cost nothing per token)."""
    price = MODEL_PRICES.get(model_id)
    if price is None:
        return 0.0
    misses = max(prompt_tokens - cache_hit_tokens, 0)
    return (
        misses * price.input_cache_miss
        + cache_hit_tokens * price.input_cache_hit
        + completion_tokens * price.output
    ) / 1_000_000
