"""Arq task that performs a dispatch. Lives inside continuum/guard/ so that
continuum.guard.dispatch is imported only from within the guard package (CLAUDE.md I2);
continuum.workers registers this function without ever importing dispatch itself.

The job carries the signed verdict and the message as JSON. It re-verifies everything in
`send`, so a job forged or edited in Redis is refused the same way as one from bad code.
"""

from __future__ import annotations

from typing import Any

from continuum.config.settings import get_settings
from continuum.db.session import session_scope
from continuum.guard.dispatch import DispatchRefusedError, SimulatedTransport, send
from continuum.guard.models import GuardVerdict, OutboundMessage
from continuum.telemetry.logging import get_logger

logger = get_logger(__name__)


async def dispatch_job(ctx: dict[str, Any], verdict_json: str, message_json: str) -> dict[str, Any]:
    transport = ctx.setdefault("transport", SimulatedTransport())
    try:
        verdict = GuardVerdict.model_validate_json(verdict_json)
        message = OutboundMessage.model_validate_json(message_json)
        receipt = send(
            verdict,
            message,
            transport=transport,
            session_scope=session_scope,
            settings=get_settings(),
        )
    except DispatchRefusedError as refusal:
        logger.warning("dispatch.refused", reason=refusal.reason.value)
        return {"status": "refused", "reason": refusal.reason.value}
    except ValueError as exc:  # malformed job payload
        logger.warning("dispatch.malformed", error=type(exc).__name__)
        return {"status": "refused", "reason": "malformed_job"}
    return {
        "status": "duplicate" if receipt.duplicate else "sent",
        "contact_id": str(receipt.contact_id),
    }
