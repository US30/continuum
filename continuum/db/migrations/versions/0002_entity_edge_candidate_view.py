"""entity_edge_candidate view — one-hop network materialisation source

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-29

CLAUDE.md §8 Phase 2 done-when: "one-hop network materialisation as a Postgres view."
This view computes candidate edges between *entities* (not records) by looking at every
pair of that entity's member party_records against every other entity's — an edge exists
if any member-record pair shares an address, shares a phone, or has a highly similar name
(pg_trgm). continuum.resolution.network reads this view nightly and replaces the contents
of the `entity_edge` table with it (CLAUDE.md §6: "Materialised nightly").

This is an O(n^2) self-join by design, acceptable at the batch scale CLAUDE.md commits to
(nightly, not real-time — CLAUDE.md §3 explicitly scopes out real-time re-screening).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

CREATE_VIEW = """
CREATE VIEW entity_edge_candidate AS
WITH member_attrs AS (
    SELECT em.entity_id, pr.full_name, pr.address_line, pr.postal_code, pr.phone
    FROM entity_member em
    JOIN party_record pr ON pr.record_id = em.record_id
)
SELECT
    LEAST(a.entity_id, b.entity_id) AS entity_id_a,
    GREATEST(a.entity_id, b.entity_id) AS entity_id_b,
    'shared_address' AS edge_type,
    1.0::numeric(5, 4) AS strength
FROM member_attrs a
JOIN member_attrs b
    ON a.entity_id <> b.entity_id
   AND a.address_line = b.address_line
   AND a.postal_code = b.postal_code
WHERE a.address_line IS NOT NULL AND a.postal_code IS NOT NULL

UNION

SELECT
    LEAST(a.entity_id, b.entity_id),
    GREATEST(a.entity_id, b.entity_id),
    'shared_phone',
    1.0::numeric(5, 4)
FROM member_attrs a
JOIN member_attrs b
    ON a.entity_id <> b.entity_id
   AND a.phone = b.phone
WHERE a.phone IS NOT NULL

UNION

SELECT
    LEAST(a.entity_id, b.entity_id),
    GREATEST(a.entity_id, b.entity_id),
    'name_similarity',
    similarity(a.full_name, b.full_name)::numeric(5, 4)
FROM member_attrs a
JOIN member_attrs b
    ON a.entity_id <> b.entity_id
   -- 0.6 is pg_trgm's own commonly-cited starting point for "plausibly similar", not
   -- tuned against this dataset yet (CLAUDE.md §10) -- revisit once Phase 7's eval
   -- harness can score network edges against something, not just resolution clusters.
   AND similarity(a.full_name, b.full_name) > 0.6
"""


def upgrade() -> None:
    op.execute(CREATE_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW entity_edge_candidate")
