"""LiteLLM gateway client, routing, retries, circuit breaker.

CLAUDE.md I7: all model calls go through the LiteLLM gateway. No provider SDK
(openai, anthropic, ollama, ...) may be imported anywhere outside this package.
See tests/test_invariants.py for the enforced import boundary.

continuum.llm.client.complete_structured is the one entrypoint everything else calls.
mypy --strict applies (CLAUDE.md §10).
"""
