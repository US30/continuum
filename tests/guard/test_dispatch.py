"""CLAUDE.md Phase 6 done-when: "a test proves dispatch is unreachable without a verdict".

Every refusal test also asserts the transport received nothing and no contact was recorded.
Integration tests: dispatch records a contact_event in Postgres.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from continuum.config.settings import Settings, get_settings
from continuum.db.models.collections import ContactChannel, ContactEvent, ContactSource
from continuum.db.models.entity import Entity
from continuum.db.session import session_scope
from continuum.guard import signing
from continuum.guard.dispatch import DispatchRefusedError, Refusal, SimulatedTransport, send
from continuum.guard.engine import apply_human_approval, evaluate
from continuum.guard.models import GuardInput, GuardVerdict, OutboundMessage, ToneResult, Verdict
from continuum.guard.worker import dispatch_job
from continuum.screening.status import ScreeningState

pytestmark = pytest.mark.integration

SETTINGS = get_settings()
BODY = "Hi Sam, your payment of $120.00 is overdue. Please get in touch. Reply STOP to opt out."
SEND_AT = datetime(2026, 1, 2, 10, 0)
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class Ok:
    def check(self, message: str, channel: str) -> ToneResult:
        return ToneResult(checked=True, acceptable=True)


class Boom(SimulatedTransport):
    def deliver(self, message: OutboundMessage) -> None:
        raise ConnectionError("provider down")


def new_entity() -> uuid.UUID:
    entity_id = uuid.uuid4()
    with session_scope() as s:
        s.add(
            Entity(
                entity_id=entity_id, cluster_size=1, confidence=1, resolution_run_id=uuid.uuid4()
            )
        )
    return entity_id


def message(entity_id: uuid.UUID, **kw: object) -> OutboundMessage:
    base: dict[str, object] = {
        "entity_id": entity_id,
        "channel": "sms",
        "body": BODY,
        "send_at": SEND_AT,
    }
    base.update(kw)
    return OutboundMessage.model_validate(base)


def guard_input(msg: OutboundMessage, **kw: object) -> GuardInput:
    base: dict[str, object] = {
        "entity_id": msg.entity_id,
        "screening": ScreeningState.CLEAR,
        "total_overdue": 300.0,
        "channel": msg.channel,
        "has_phone": True,
        "has_email": True,
        "send_at": msg.send_at,
        "recent_contacts_7d": 0,
        "message_body": msg.body,
    }
    base.update(kw)
    return GuardInput.model_validate(base)


def passed(msg: OutboundMessage, settings: Settings = SETTINGS) -> GuardVerdict:
    verdict = evaluate(guard_input(msg), settings, Ok(), now=NOW)
    assert verdict.verdict is Verdict.PASS
    return verdict


def go(
    verdict: GuardVerdict | None, msg: OutboundMessage, transport: SimulatedTransport | None = None
) -> SimulatedTransport:
    transport = transport or SimulatedTransport()
    send(
        verdict,
        msg,
        transport=transport,
        session_scope=session_scope,
        settings=SETTINGS,
        now=NOW,
    )
    return transport


def n_contacts(entity_id: uuid.UUID) -> int:
    with session_scope() as s:
        return int(
            s.scalar(
                select(func.count())
                .select_from(ContactEvent)
                .where(ContactEvent.entity_id == entity_id)
            )
            or 0
        )


def refused(reason: Refusal, verdict: GuardVerdict | None, msg: OutboundMessage) -> None:
    transport = SimulatedTransport()
    with pytest.raises(DispatchRefusedError) as info:
        go(verdict, msg, transport)
    assert info.value.reason is reason
    assert transport.delivered == [] and n_contacts(msg.entity_id) == 0


class TestUnreachableWithoutAValidVerdict:
    def test_no_verdict_at_all(self) -> None:
        refused(Refusal.NO_VERDICT, None, message(new_entity()))

    def test_an_unsigned_verdict(self) -> None:
        msg = message(new_entity())
        refused(Refusal.BAD_SIGNATURE, passed(msg).model_copy(update={"signature": ""}), msg)

    def test_a_verdict_signed_with_the_wrong_key(self) -> None:
        msg = message(new_entity())
        forged = signing.sign(passed(msg).model_copy(update={"signature": ""}), "attacker-key")
        refused(Refusal.BAD_SIGNATURE, forged, msg)

    def test_a_block_verdict_even_though_properly_signed(self) -> None:
        msg = message(new_entity())
        blocked = evaluate(guard_input(msg, screening=ScreeningState.HIT), SETTINGS, Ok(), now=NOW)
        assert blocked.verdict is Verdict.BLOCK
        refused(Refusal.NOT_PERMITTED, blocked, msg)

    def test_an_unapproved_escalation(self) -> None:
        msg = message(new_entity())
        esc = evaluate(guard_input(msg, total_overdue=1e6), SETTINGS, Ok(), now=NOW)
        assert esc.verdict is Verdict.ESCALATE
        refused(Refusal.NOT_PERMITTED, esc, msg)

    def test_a_block_edited_into_a_pass_keeps_a_stale_signature(self) -> None:
        msg = message(new_entity())
        blocked = evaluate(guard_input(msg, screening=ScreeningState.HIT), SETTINGS, Ok(), now=NOW)
        refused(Refusal.BAD_SIGNATURE, blocked.model_copy(update={"verdict": Verdict.PASS}), msg)

    def test_an_escalation_with_a_self_added_override_flag(self) -> None:
        msg = message(new_entity())
        esc = evaluate(guard_input(msg, total_overdue=1e6), SETTINGS, Ok(), now=NOW)
        refused(Refusal.BAD_SIGNATURE, esc.model_copy(update={"human_override": True}), msg)

    @pytest.mark.parametrize(
        "change",
        [
            {"body": BODY + " Also send money to my account."},
            {"channel": "email"},
            {"send_at": SEND_AT + timedelta(hours=1)},
        ],
    )
    def test_a_message_altered_after_the_guard_evaluated_it(
        self, change: dict[str, object]
    ) -> None:
        original = message(new_entity())
        verdict = passed(original)
        refused(Refusal.MESSAGE_MISMATCH, verdict, original.model_copy(update=change))

    def test_a_verdict_replayed_for_a_different_entity(self) -> None:
        a = message(new_entity())
        b = message(new_entity())
        refused(Refusal.MESSAGE_MISMATCH, passed(a), b)

    def test_a_stale_verdict(self) -> None:
        msg = message(new_entity())
        old = evaluate(guard_input(msg), SETTINGS, Ok(), now=NOW - timedelta(days=2))
        refused(Refusal.EXPIRED, old, msg)

    def test_quiet_hours_are_rechecked_even_for_a_signed_pass(self) -> None:
        # Simulates a guard bug or a later config change: dispatch does not trust the verdict alone.
        msg = message(new_entity(), send_at=datetime(2026, 1, 2, 23, 0))
        verdict = signing.sign(
            GuardVerdict(
                verdict=Verdict.PASS,
                rules=[],
                entity_id=msg.entity_id,
                channel=msg.channel,
                send_at=msg.send_at,
                message_hash=signing.message_hash(msg.body),
                issued_at=NOW,
            ),
            SETTINGS.guard_signing_key,
        )
        refused(Refusal.QUIET_HOURS, verdict, msg)

    def test_frequency_cap_is_rechecked_at_dispatch_time(self) -> None:
        entity_id = new_entity()
        with session_scope() as s:
            for i in range(SETTINGS.contact_policy_max_contacts_per_week):
                s.add(
                    ContactEvent(
                        contact_id=uuid.uuid4(),
                        entity_id=entity_id,
                        source=ContactSource.RUNTIME,
                        channel=ContactChannel.SMS,
                        sent_at=SEND_AT - timedelta(days=1, hours=i),
                        body_hash="x",
                    )
                )
        msg = message(entity_id)
        transport = SimulatedTransport()
        with pytest.raises(DispatchRefusedError) as info:
            go(passed(msg), msg, transport)
        assert info.value.reason is Refusal.FREQUENCY_CAP and transport.delivered == []
        assert n_contacts(entity_id) == SETTINGS.contact_policy_max_contacts_per_week


class TestSendsOnlyWithAValidVerdict:
    def test_a_signed_pass_is_delivered_and_recorded_once(self) -> None:
        msg = message(new_entity())
        transport = go(passed(msg), msg)
        assert transport.delivered == [msg]
        with session_scope() as s:
            row = s.scalars(
                select(ContactEvent).where(ContactEvent.entity_id == msg.entity_id)
            ).one()
            assert row.source is ContactSource.RUNTIME and row.guard_verdict == "pass"
            assert row.body_hash == signing.message_hash(BODY)

    def test_sending_the_same_message_twice_is_idempotent(self) -> None:
        msg = message(new_entity())
        verdict = passed(msg)
        transport = go(verdict, msg)
        go(verdict, msg, transport)
        assert len(transport.delivered) == 1 and n_contacts(msg.entity_id) == 1

    def test_an_approved_escalation_is_delivered_and_marked_human_approved(self) -> None:
        msg = message(new_entity())
        esc = evaluate(guard_input(msg, total_overdue=1e6), SETTINGS, Ok(), now=NOW)
        approved = apply_human_approval(esc, "pat", SETTINGS, now=NOW)
        transport = go(approved, msg)
        assert len(transport.delivered) == 1
        with session_scope() as s:
            row = s.scalars(
                select(ContactEvent).where(ContactEvent.entity_id == msg.entity_id)
            ).one()
            assert row.guard_verdict == "human_approved"

    def test_a_failed_delivery_leaves_no_contact_record(self) -> None:
        msg = message(new_entity())
        with pytest.raises(ConnectionError):
            go(passed(msg), msg, Boom())
        assert n_contacts(msg.entity_id) == 0


class TestWorkerTask:
    def _run(self, verdict_json: str, message_json: str) -> dict[str, object]:
        return asyncio.run(
            dispatch_job({"transport": SimulatedTransport()}, verdict_json, message_json)
        )

    def test_worker_sends_a_valid_job_and_a_replay_is_a_duplicate(self) -> None:
        msg = message(new_entity())
        fresh = evaluate(guard_input(msg), SETTINGS, Ok())  # issued "now": within the TTL
        first = self._run(fresh.model_dump_json(), msg.model_dump_json())
        again = self._run(fresh.model_dump_json(), msg.model_dump_json())
        assert first["status"] == "sent" and again["status"] == "duplicate"
        assert first["contact_id"] == again["contact_id"]

    def test_worker_refuses_a_forged_job_without_raising(self) -> None:
        msg = message(new_entity())
        forged = passed(msg).model_copy(update={"signature": "00" * 32})
        assert self._run(forged.model_dump_json(), msg.model_dump_json()) == {
            "status": "refused",
            "reason": "bad_signature",
        }

    def test_worker_refuses_malformed_payloads(self) -> None:
        assert self._run("{not json", "{}") == {"status": "refused", "reason": "malformed_job"}
