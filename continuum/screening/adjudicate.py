"""LLM adjudicator for the ambiguous screening band.

CLAUDE.md §8: "Adjudicator node via LiteLLM returning structured {verdict, confidence,
reasoning}." CLAUDE.md I2: continuum.screening.bands' deterministic band logic already ran
before a candidate ever reaches this module — adjudicate() has no way to see a candidate
that band_for_score didn't already route to ScreeningBand.ADJUDICATE, so this module can
never widen the deterministic gate. CLAUDE.md I7: the only model call in this module goes
through continuum.llm.client, never a provider SDK directly.

CLAUDE.md §10: "Prompts live in files, not string literals in logic, and are versioned. The
version goes into screening_decision.model_version." See continuum/screening/prompts/.
"""

from __future__ import annotations

from pathlib import Path

from continuum.llm.client import complete_structured
from continuum.screening.candidates import ScreeningCandidate
from continuum.screening.schemas import AdjudicationVerdict

PROMPT_DIR = Path(__file__).parent / "prompts"
PROMPT_VERSION = "adjudicate_v2"

_SYSTEM_PROMPT = (PROMPT_DIR / f"{PROMPT_VERSION}_system.txt").read_text(encoding="utf-8")
_USER_PROMPT_TEMPLATE = (PROMPT_DIR / f"{PROMPT_VERSION}_user.txt").read_text(encoding="utf-8")


def adjudicate(candidate: ScreeningCandidate) -> AdjudicationVerdict:
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        customer_name=candidate.entity_name,
        customer_dob=candidate.entity_date_of_birth or "unknown",
        listing_name=candidate.listing_primary_name,
        matched_alias=candidate.matched_alias,
        listing_dob=candidate.listing_birth_date or "unknown",
        listing_countries=candidate.listing_countries or "unknown",
        listing_topics=candidate.listing_topics or "unknown",
        similarity=candidate.name_similarity,
        dob_evidence=candidate.dob_evidence.value,
    )
    return complete_structured(
        node="screening_adjudicator",
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        response_model=AdjudicationVerdict,
    )


def model_version(model_id: str) -> str:
    """Recorded into screening_decision.model_version — model id plus prompt version, so
    a prompt change is traceable the same way a model swap is (CLAUDE.md §10, I7).
    """
    return f"{model_id}::{PROMPT_VERSION}"
