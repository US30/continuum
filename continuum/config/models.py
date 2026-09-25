"""Logical-node -> model-id routing table.

CLAUDE.md I7: "Model choice is configuration... a swap is a config change." Every LLM call
site in continuum/graph and continuum/screening looks up its model by logical node name
through continuum.llm, which reads this table — never a hardcoded model id at the call site.

Model assignments follow CLAUDE.md §4:
    DeepSeek-V4        — prioritisation, strategy   (low volume)
    Llama 3.2 8B       — screening adjudication, guard tone check (main volume)
    Llama 3 8B 4-bit   — message drafting (cheapest tier)
"""

from __future__ import annotations

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
    "screening_adjudicator": "deepseek/deepseek-chat",
    "prioritise": "ollama/deepseek-v4",
    "strategy": "ollama/deepseek-v4",
    "draft": "ollama/llama3:8b-q4_0",
    "guard_tone": "ollama/llama3.2:8b",
}


def model_for(node: LogicalNode) -> str:
    """Resolve a logical node name to its configured model id."""
    return MODEL_ROUTING[node]
