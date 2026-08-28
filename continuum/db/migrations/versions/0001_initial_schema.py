"""initial schema — all CLAUDE.md §6 tables

Revision ID: 0001
Revises:
Create Date: 2026-08-28

All tables from CLAUDE.md §6 are created together in one migration, even though several
stay empty until later phases (entity, entity_member, entity_edge: Phase 2; screening_*:
Phase 3; risk_file_event, case, contact_event: Phases 3/5/6). Creating them up front keeps
every foreign key coherent from the start and avoids a fragile migration chain in the
first weeks of the build. Migrations are forward-only (CLAUDE.md §10) — this one is never
edited after being applied; schema changes are new revisions.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent: CREATE EXTENSION IF NOT EXISTS is safe to run against a DB where
    # docker/postgres/init.sql already created these (I6 — every batch/migration step
    # must be re-runnable).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ---------------------------------------------------------------- party_record
    op.create_table(
        "party_record",
        sa.Column("record_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_system", sa.String(64), nullable=False),
        sa.Column("source_record_id", sa.String(128), nullable=False),
        sa.Column("full_name", sa.String(256), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column("national_id", sa.String(64), nullable=True),
        sa.Column("address_line", sa.String(256), nullable=True),
        sa.Column("postal_code", sa.String(16), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("email", sa.String(256), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_party_record_source_natural_key",
        "party_record",
        ["source_system", "source_record_id"],
        unique=True,
    )
    op.execute(
        "CREATE INDEX ix_party_record_name_trgm ON party_record "
        "USING gin (full_name gin_trgm_ops)"
    )

    # -------------------------------------------------------------------- entity
    op.create_table(
        "entity",
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("cluster_size", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("resolution_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "entity_member",
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entity.entity_id", name="fk_entity_member_entity_id_entity"),
            primary_key=True,
        ),
        sa.Column(
            "record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("party_record.record_id", name="fk_entity_member_record_id_party_record"),
            primary_key=True,
        ),
        sa.Column("match_probability", sa.Numeric(5, 4), nullable=False),
    )

    op.create_table(
        "entity_edge",
        sa.Column("edge_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entity_id_a",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entity.entity_id", name="fk_entity_edge_entity_id_a_entity"),
            nullable=False,
        ),
        sa.Column(
            "entity_id_b",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entity.entity_id", name="fk_entity_edge_entity_id_b_entity"),
            nullable=False,
        ),
        sa.Column(
            "edge_type",
            sa.Enum(
                "shared_address",
                "shared_phone",
                "name_similarity",
                name="edge_type",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("strength", sa.Numeric(5, 4), nullable=False),
        sa.Column(
            "materialised_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_entity_edge_a", "entity_edge", ["entity_id_a"])
    op.create_index("ix_entity_edge_b", "entity_edge", ["entity_id_b"])

    # -------------------------------------------------------------------- account
    delinquency_bucket = sa.Enum(
        "current",
        "30_dpd",
        "60_dpd",
        "90_dpd",
        "cured",
        "charged_off",
        name="delinquency_bucket",
        native_enum=False,
        length=16,
    )

    op.create_table(
        "account",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entity.entity_id", name="fk_account_entity_id_entity"),
            nullable=False,
        ),
        sa.Column("product", sa.String(32), nullable=False),
        sa.Column("exposure", sa.Numeric(12, 2), nullable=False),
        sa.Column("origination_date", sa.Date(), nullable=False),
        sa.Column("current_state", delinquency_bucket, nullable=False, server_default="current"),
    )
    op.create_index("ix_account_entity_id", "account", ["entity_id"])

    op.create_table(
        "payment_event",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.account_id", name="fk_payment_event_account_id_account"),
            nullable=False,
        ),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("paid_date", sa.Date(), nullable=True),
        sa.Column("amount_due", sa.Numeric(12, 2), nullable=False),
        sa.Column("amount_paid", sa.Numeric(12, 2), nullable=False, server_default="0"),
    )
    op.create_index("ix_payment_event_account_due", "payment_event", ["account_id", "due_date"])

    op.create_table(
        "delinquency_state",
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.account_id", name="fk_delinquency_state_account_id_account"),
            primary_key=True,
        ),
        sa.Column("effective_from", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", delinquency_bucket, nullable=False),
        sa.Column("days_past_due", sa.Integer(), nullable=False, server_default="0"),
    )
    # Composite index called out explicitly in CLAUDE.md §6.
    op.create_index(
        "ix_delinquency_state_account_effective_from",
        "delinquency_state",
        ["account_id", "effective_from"],
    )
    # Partial unique index: at most one open (effective_to IS NULL) row per account.
    op.execute(
        "CREATE UNIQUE INDEX uq_delinquency_state_open_row ON delinquency_state (account_id) "
        "WHERE effective_to IS NULL"
    )

    # ---------------------------------------------------------------- sanctions_*
    op.create_table(
        "sanctions_entity",
        sa.Column("listing_id", sa.String(128), primary_key=True),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("primary_name", sa.String(256), nullable=False),
        sa.Column("schema_type", sa.String(32), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("countries", sa.String(256), nullable=True),
        sa.Column("topics", sa.String(256), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_sanctions_entity_snapshot", "sanctions_entity", ["snapshot_id"])

    op.create_table(
        "sanctions_alias",
        sa.Column("alias_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "listing_id",
            sa.String(128),
            sa.ForeignKey(
                "sanctions_entity.listing_id", name="fk_sanctions_alias_listing_id_sanctions_entity"
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(256), nullable=False),
    )
    op.create_index("ix_sanctions_alias_listing_id", "sanctions_alias", ["listing_id"])
    op.execute(
        "CREATE INDEX ix_sanctions_alias_name_trgm ON sanctions_alias USING gin (name gin_trgm_ops)"
    )

    op.create_table(
        "screening_decision",
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entity.entity_id", name="fk_screening_decision_entity_id_entity"),
            nullable=False,
        ),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "method",
            sa.Enum(
                "auto_confirm",
                "auto_reject",
                "adjudicated",
                "human",
                name="screening_method",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("model_version", sa.String(64), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_screening_decision_entity_id", "screening_decision", ["entity_id"])

    # ----------------------------------------------------------- risk_file_event
    op.create_table(
        "risk_file_event",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entity.entity_id", name="fk_risk_file_event_entity_id_entity"),
            nullable=False,
        ),
        sa.Column(
            "event_type",
            sa.Enum(
                "screening_outcome",
                "delinquency_transition",
                "network_update",
                "case_disposition",
                name="risk_file_event_type",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_risk_file_event_entity_recorded", "risk_file_event", ["entity_id", "recorded_at"]
    )
    # Partial index: the guard's hot path (CLAUDE.md §6) — latest screening outcome per entity.
    op.execute(
        "CREATE INDEX ix_risk_file_event_screening_outcome ON risk_file_event "
        "(entity_id, recorded_at) WHERE event_type = 'screening_outcome'"
    )

    # ------------------------------------------------------------- case / contact
    op.create_table(
        "case",
        sa.Column("case_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entity.entity_id", name="fk_case_entity_id_entity"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("expected_recovery", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "disposition",
            sa.Enum(
                "open",
                "resolved",
                "escalated",
                "diverted",
                name="case_disposition",
                native_enum=False,
                length=16,
            ),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_case_entity_id", "case", ["entity_id"])

    op.create_table(
        "contact_event",
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Nullable: simulated-history rows (Phase 1) predate case assembly (Phase 5).
        sa.Column(
            "case_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("case.case_id", name="fk_contact_event_case_id_case"),
            nullable=True,
        ),
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entity.entity_id", name="fk_contact_event_entity_id_entity"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.account_id", name="fk_contact_event_account_id_account"),
            nullable=True,
        ),
        sa.Column(
            "source",
            sa.Enum(
                "simulated_history",
                "runtime",
                name="contact_source",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "channel",
            sa.Enum("sms", "email", name="contact_channel", native_enum=False, length=8),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("body_hash", sa.String(64), nullable=False),
        sa.Column("guard_verdict", sa.String(32), nullable=True),
        sa.Column("responded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_contact_event_entity_sent_at", "contact_event", ["entity_id", "sent_at"])

    # ------------------------------------------------------------ ground_truth_link
    # CLAUDE.md I4: eval-only. Only continuum.foundation.collisions writes it; only eval/
    # reads it. See continuum/db/models/eval.py and tests/test_invariants.py.
    op.create_table(
        "ground_truth_link",
        sa.Column("link_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "record_id_a",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("party_record.record_id", name="fk_ground_truth_link_record_id_a_party_record"),
            nullable=False,
        ),
        sa.Column(
            "record_id_b",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("party_record.record_id", name="fk_ground_truth_link_record_id_b_party_record"),
            nullable=False,
        ),
        sa.Column(
            "collision_type",
            sa.Enum(
                "same_person",
                "hard_negative",
                name="collision_type",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "planted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("ground_truth_link")
    op.drop_table("contact_event")
    op.drop_table("case")
    op.drop_table("risk_file_event")
    op.drop_table("screening_decision")
    op.drop_table("sanctions_alias")
    op.drop_table("sanctions_entity")
    op.drop_table("delinquency_state")
    op.drop_table("payment_event")
    op.drop_table("account")
    op.drop_table("entity_edge")
    op.drop_table("entity_member")
    op.drop_table("entity")
    op.drop_table("party_record")
