"""Adjudicator wiring with the LLM stubbed out — no network, no key."""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from continuum.llm import client
from continuum.screening import adjudicate as adj
from continuum.screening.candidates import ScreeningCandidate
from continuum.screening.schemas import AdjudicationVerdict


def _candidate() -> ScreeningCandidate:
    return ScreeningCandidate(
        entity_id=uuid.uuid4(),
        listing_id="synth-00001",
        listing_primary_name="Aldric Vantessen",
        matched_alias="Aldric Vantessen",
        entity_name="Aldric Vantessen",
        entity_date_of_birth=date(1958, 3, 12),
        listing_birth_date=None,
        listing_countries="zz",
        listing_topics="sanction",
        name_similarity=0.97,
    )


def test_adjudicate_sends_evidence_through_the_gateway_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake(node, system_prompt, user_prompt, response_model, **_):  # type: ignore[no-untyped-def]
        seen.update(node=node, user=user_prompt, system=system_prompt, model=response_model)
        return AdjudicationVerdict(verdict="reject", confidence=0.3, reasoning="stub")

    monkeypatch.setattr(adj, "complete_structured", fake)
    verdict = adj.adjudicate(_candidate())

    assert verdict.verdict == "reject"
    assert seen["node"] == "screening_adjudicator"
    assert seen["model"] is AdjudicationVerdict
    user = str(seen["user"])
    assert "Aldric Vantessen" in user and "unknown" in user  # missing listing DOB is explicit
    assert "0.970" in user


def test_model_version_records_model_and_prompt_version() -> None:
    assert adj.model_version("deepseek/deepseek-chat") == "deepseek/deepseek-chat::adjudicate_v2"


def test_prompt_files_for_the_current_version_exist() -> None:
    assert (adj.PROMPT_DIR / f"{adj.PROMPT_VERSION}_system.txt").is_file()
    assert (adj.PROMPT_DIR / f"{adj.PROMPT_VERSION}_user.txt").is_file()


class TestApiKeyRouting:
    def test_gateway_key_wins(self) -> None:
        assert client._api_key_for("deepseek/deepseek-chat", "gw", "ds") == "gw"

    def test_deepseek_model_uses_deepseek_key(self) -> None:
        assert client._api_key_for("deepseek/deepseek-chat", "", "ds") == "ds"

    def test_other_provider_gets_none(self) -> None:
        assert client._api_key_for("ollama/llama3.1:8b", "", "ds") is None

    def test_no_keys_is_none(self) -> None:
        assert client._api_key_for("deepseek/deepseek-chat", "", "") is None
