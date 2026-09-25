"""Structured outputs the LLM nodes must return (parsed by continuum.llm.client)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FraudAdjudication(BaseModel):
    divert: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class StrategyChoice(BaseModel):
    option_index: int
    rationale: str


class DraftOutput(BaseModel):
    body: str
