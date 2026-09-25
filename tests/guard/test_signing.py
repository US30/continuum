from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from continuum.guard import signing
from continuum.guard.models import GuardVerdict, Verdict

KEY = "test-key"


def verdict(**kw: object) -> GuardVerdict:
    base: dict[str, object] = {
        "verdict": Verdict.PASS,
        "rules": [],
        "entity_id": uuid.uuid4(),
        "case_id": uuid.uuid4(),
        "channel": "sms",
        "send_at": datetime(2026, 1, 2, 10, tzinfo=UTC),
        "message_hash": signing.message_hash("hello"),
        "issued_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(kw)
    return GuardVerdict.model_validate(base)


def test_a_signed_verdict_verifies() -> None:
    assert signing.verify(signing.sign(verdict(), KEY), KEY)


def test_unsigned_and_wrong_key_do_not_verify() -> None:
    assert not signing.verify(verdict(), KEY)
    assert not signing.verify(signing.sign(verdict(), "other-key"), KEY)


@pytest.mark.parametrize(
    "field,value",
    [
        ("verdict", Verdict.BLOCK),
        ("human_override", True),
        ("approved_by", "mallory"),
        ("entity_id", uuid.uuid4()),
        ("channel", "email"),
        ("send_at", datetime(2026, 1, 3, 10, tzinfo=UTC)),
        ("message_hash", signing.message_hash("different text")),
        ("issued_at", datetime(2026, 6, 1, tzinfo=UTC)),
        ("case_id", uuid.uuid4()),
    ],
)
def test_editing_any_signed_field_breaks_the_signature(field: str, value: object) -> None:
    signed = signing.sign(verdict(), KEY)
    assert not signing.verify(signed.model_copy(update={field: value}), KEY)


def test_unsigned_informational_fields_may_change() -> None:
    signed = signing.sign(verdict(), KEY)
    assert signing.verify(signed.model_copy(update={"rules": [], "tone": None}), KEY)


def test_message_hash_is_sensitive_to_every_character() -> None:
    assert signing.message_hash("a") != signing.message_hash("a ")
    assert signing.message_hash("x") == signing.message_hash("x")
