"""Engine and session factory.

Usage:

    from continuum.db.session import session_scope

    with session_scope() as session:
        session.add(obj)
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from continuum.config.settings import get_settings

_engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)


def get_engine() -> Engine:
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional session: commits on clean exit, rolls back on exception."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
