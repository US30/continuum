"""HMAC signing of guard verdicts.

Why: "dispatch is unreachable without a guard verdict" (CLAUDE.md I2, Phase 6 done-when) is
only meaningful if a verdict cannot be conjured or edited by other code. A signature over
the verdict, the entity, the channel, the send time and the hash of the exact message makes
any of those changes detectable; the key never leaves this package's settings.

The payload deliberately excludes `rules`, `tone` and usage (informational, may be large) but
includes `human_override` and `approved_by`, so approval cannot be added without the key.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from continuum.guard.models import GuardVerdict


def message_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _payload(verdict: GuardVerdict) -> bytes:
    return json.dumps(
        {
            "verdict": verdict.verdict.value,
            "human_override": verdict.human_override,
            "approved_by": verdict.approved_by,
            "entity_id": str(verdict.entity_id),
            "case_id": str(verdict.case_id) if verdict.case_id else None,
            "channel": verdict.channel,
            "send_at": verdict.send_at.isoformat() if verdict.send_at else None,
            "message_hash": verdict.message_hash,
            "issued_at": verdict.issued_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign(verdict: GuardVerdict, key: str) -> GuardVerdict:
    signature = hmac.new(key.encode("utf-8"), _payload(verdict), hashlib.sha256).hexdigest()
    return verdict.model_copy(update={"signature": signature})


def verify(verdict: GuardVerdict, key: str) -> bool:
    if not verdict.signature:
        return False
    expected = hmac.new(key.encode("utf-8"), _payload(verdict), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, verdict.signature)
