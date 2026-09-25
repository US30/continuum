"""The single egress point for outbound customer messages.

CLAUDE.md I2: "The compliance guard is the single egress point. No code path may dispatch
an outbound message without a guard verdict." `send()` is the only function in the codebase
that hands a message to a transport, and it refuses unless ALL of these hold:

  1. a verdict is supplied at all;
  2. its HMAC signature verifies (it was produced by continuum.guard.engine, unedited);
  3. it permits dispatch: PASS, or an ESCALATE a human approved — never BLOCK;
  4. it was issued for *this* message: same entity, channel, send time, and the SHA-256 of the
     body equals the signed message_hash (so the text cannot be swapped after evaluation);
  5. it is not stale (Settings.guard_verdict_ttl_seconds);
  6. the world still allows it: not in quiet hours at send time, and the entity is still under
     the weekly frequency cap when the contact is recorded — re-checked here, inside the
     transaction that records it, because the guard's check may be hours old.

tests/test_invariants.py fails the build if any module outside continuum/guard/ imports this
one; the graph only produces a DispatchRequest and the worker layer forwards it by task name.
No real provider exists in this synthetic system (I8): SimulatedTransport records the send.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from continuum.config.settings import Settings
from continuum.db.models.collections import ContactChannel, ContactEvent, ContactSource
from continuum.guard import signing
from continuum.guard.models import GuardVerdict, OutboundMessage
from continuum.guard.rules import in_quiet_hours

CONTACT_ID_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-0000000000d6")
FREQUENCY_WINDOW_DAYS = 7


class Refusal(StrEnum):
    NO_VERDICT = "no_verdict"
    BAD_SIGNATURE = "bad_signature"
    NOT_PERMITTED = "verdict_does_not_permit_dispatch"
    MESSAGE_MISMATCH = "message_does_not_match_verdict"
    EXPIRED = "verdict_expired"
    QUIET_HOURS = "quiet_hours"
    FREQUENCY_CAP = "frequency_cap"


class DispatchRefusedError(RuntimeError):
    def __init__(self, reason: Refusal, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason


class Transport(Protocol):
    def deliver(self, message: OutboundMessage) -> None: ...


@dataclass
class SimulatedTransport:
    """Stands in for an SMS/email provider. Nothing leaves the process (I8)."""

    delivered: list[OutboundMessage] = field(default_factory=list)

    def deliver(self, message: OutboundMessage) -> None:
        self.delivered.append(message)


@dataclass(frozen=True)
class Receipt:
    contact_id: uuid.UUID
    duplicate: bool  # True if this exact message was already dispatched (idempotent no-op)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _check_verdict(
    verdict: GuardVerdict | None, message: OutboundMessage, settings: Settings, now: datetime
) -> GuardVerdict:
    if verdict is None:
        raise DispatchRefusedError(Refusal.NO_VERDICT, "dispatch requires a guard verdict")
    if not signing.verify(verdict, settings.guard_signing_key):
        raise DispatchRefusedError(Refusal.BAD_SIGNATURE)
    if not verdict.permits_dispatch:
        raise DispatchRefusedError(
            Refusal.NOT_PERMITTED,
            f"verdict is {verdict.verdict.value}, human_override={verdict.human_override}",
        )
    if (
        verdict.entity_id != message.entity_id
        or verdict.channel != message.channel
        or verdict.send_at != message.send_at
        or verdict.message_hash != signing.message_hash(message.body)
    ):
        raise DispatchRefusedError(Refusal.MESSAGE_MISMATCH)
    age = now - _aware(verdict.issued_at)
    if age > timedelta(seconds=settings.guard_verdict_ttl_seconds) or age < timedelta(seconds=-60):
        raise DispatchRefusedError(Refusal.EXPIRED, f"verdict age {age}")
    return verdict


def send(
    verdict: GuardVerdict | None,
    message: OutboundMessage,
    *,
    transport: Transport,
    session_scope: Callable[[], AbstractContextManager[Session]],
    settings: Settings,
    now: datetime | None = None,
) -> Receipt:
    now = _aware(now or datetime.now(UTC))
    checked = _check_verdict(verdict, message, settings, now)

    if in_quiet_hours(message.send_at, settings):
        raise DispatchRefusedError(Refusal.QUIET_HOURS, message.send_at.isoformat())

    contact_id = uuid.uuid5(
        CONTACT_ID_NAMESPACE, f"{message.entity_id}:{checked.message_hash}:{message.send_at}"
    )
    with session_scope() as session:
        if session.get(ContactEvent, contact_id) is not None:
            return Receipt(contact_id, duplicate=True)  # already sent: idempotent (I6)

        window_start = message.send_at - timedelta(days=FREQUENCY_WINDOW_DAYS)
        recent = session.scalar(
            select(func.count())
            .select_from(ContactEvent)
            .where(
                ContactEvent.entity_id == message.entity_id,
                ContactEvent.sent_at > window_start,
                ContactEvent.sent_at <= message.send_at,
            )
        )
        if int(recent or 0) >= settings.contact_policy_max_contacts_per_week:
            raise DispatchRefusedError(Refusal.FREQUENCY_CAP, f"{recent} contacts in window")

        # Record first, deliver second, inside one transaction: if delivery raises, the
        # rollback removes the record, so a failed send is never logged as a contact.
        session.execute(
            pg_insert(ContactEvent)
            .values(
                contact_id=contact_id,
                case_id=message.case_id,
                entity_id=message.entity_id,
                account_id=None,
                source=ContactSource.RUNTIME,
                channel=ContactChannel(message.channel),
                sent_at=message.send_at,
                body_hash=checked.message_hash,
                guard_verdict=(
                    "human_approved" if checked.human_override else checked.verdict.value
                ),
                responded=False,
            )
            .on_conflict_do_nothing(index_elements=["contact_id"])
        )
        transport.deliver(message)
    return Receipt(contact_id, duplicate=False)
