"""structlog setup, correlation-id propagation, PII hashing at write.

CLAUDE.md §10: "Structured logging, correlation id propagated across batch -> queue ->
graph nodes. PII hashed at write."
"""

from __future__ import annotations

import hashlib
import logging
from contextvars import ContextVar

import structlog

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def set_correlation_id(correlation_id: str) -> None:
    _correlation_id.set(correlation_id)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def _add_correlation_id(
    logger: object, method_name: str, event_dict: dict[str, object]
) -> dict[str, object]:
    cid = _correlation_id.get()
    if cid is not None:
        event_dict["correlation_id"] = cid
    return event_dict


def hash_pii(value: str) -> str:
    """One-way hash for PII fields at the log boundary. Not for storage — for logs only."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def configure_logging(log_level: str = "INFO") -> None:
    logging.basicConfig(level=log_level.upper(), format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_correlation_id,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(log_level.upper())),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
