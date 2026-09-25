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
from continuum.llm.untrusted import sanitize_person_name, wrap
from continuum.screening.candidates import ScreeningCandidate
from continuum.screening.schemas import AdjudicationVerdict

PROMPT_DIR = Path(__file__).parent / "prompts"
PROMPT_VERSION = "adjudicate_v3"

_SYSTEM_PROMPT = (PROMPT_DIR / f"{PROMPT_VERSION}_system.txt").read_text(encoding="utf-8")
_USER_PROMPT_TEMPLATE = (PROMPT_DIR / f"{PROMPT_VERSION}_user.txt").read_text(encoding="utf-8")


class SuspiciousNameError(ValueError):
    """The customer's stored name is not a plausible name (it contains punctuation, digits or
    length no real name has). It could be an attempt to talk the adjudicator into clearing a
    true hit, so no model call is made and the candidate stays *undecided* — which keeps the
    entity's screening UNRESOLVED and blocks outreach (fail closed, CLAUDE.md I5)."""


def build_prompt(candidate: ScreeningCandidate) -> str:
    """The user prompt for one candidate. The customer name is validated as a name first
    (a hostile name is refused, not cleaned), then every free-text value is fenced as data."""
    if not sanitize_person_name(candidate.entity_name, fallback="", max_words=6, max_len=80):
        raise SuspiciousNameError("customer name is not a plausible name")
    return _USER_PROMPT_TEMPLATE.format(
        customer_name=wrap("customer_name", candidate.entity_name),
        customer_dob=candidate.entity_date_of_birth or "unknown",
        listing_name=wrap("listing_name", candidate.listing_primary_name),
        matched_alias=wrap("matched_alias", candidate.matched_alias),
        listing_dob=candidate.listing_birth_date or "unknown",
        listing_countries=wrap("countries", candidate.listing_countries or "unknown", 60),
        listing_topics=wrap("topics", candidate.listing_topics or "unknown", 60),
        similarity=candidate.name_similarity,
        dob_evidence=candidate.dob_evidence.value,
    )


def adjudicate(candidate: ScreeningCandidate) -> AdjudicationVerdict:
    return complete_structured(
        node="screening_adjudicator",
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=build_prompt(candidate),
        response_model=AdjudicationVerdict,
    )


def model_version(model_id: str) -> str:
    """Recorded into screening_decision.model_version — model id plus prompt version, so
    a prompt change is traceable the same way a model swap is (CLAUDE.md §10, I7).
    """
    return f"{model_id}::{PROMPT_VERSION}"
