"""Structured contracts for screening. CLAUDE.md §10: Pydantic v2 models for all
inter-node contracts.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AdjudicationVerdict(BaseModel):
    """CLAUDE.md §8: the adjudicator returns structured {verdict, confidence, reasoning}."""

    verdict: Literal["confirm", "reject"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
