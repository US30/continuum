from __future__ import annotations

import pytest

from continuum.graph.deps import DisabledLLM, LLMDisabledError
from continuum.graph.schemas import DraftOutput


def test_disabled_llm_raises_immediately_without_network() -> None:
    with pytest.raises(LLMDisabledError):
        DisabledLLM().complete("draft", "s", "u", DraftOutput)
