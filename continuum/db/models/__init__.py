"""ORM models, one module per table group (party, entity, account, screening, risk_file,
collections, eval). All models import their table from here so Alembic autogenerate sees
the full metadata.
"""

from continuum.db.models import (  # noqa: F401
    account,
    collections,
    entity,
    eval as eval_models,
    party,
    risk_file,
    screening,
)

__all__ = [
    "account",
    "collections",
    "entity",
    "eval_models",
    "party",
    "risk_file",
    "screening",
]
