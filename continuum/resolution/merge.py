"""Repoints Phase-1 singleton entities to resolved clusters.

Per the Phase 1 -> Phase 2 handoff (continuum.foundation.build's module docstring, agreed
with the user before this module was written): every party_record starts under its own
singleton Entity. Resolution's job is to *merge* those singletons wherever
continuum.resolution.cluster's clustering says several of them are the same real person —
never to create entities from nothing.

For each resolved cluster spanning more than one singleton entity: the survivor is the
cluster's own deterministic id (continuum.resolution.cluster.stable_cluster_id), so no
separate tie-break rule is needed. Every row in an FK'd table (account, contact_event,
risk_file_event, case) that belongs to an absorbed entity is repointed to the survivor, all
entity_member rows for the cluster's records are repointed the same way, and the absorbed
Entity rows are deleted.

Re-running on unchanged input is a no-op (CLAUDE.md I6): after the first run, the survivor's
entity_id already equals the stable cluster id, so on the second run every "absorbed" set is
empty — there is nothing left to repoint or delete.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from continuum.db.models.account import Account
from continuum.db.models.collections import Case, ContactEvent
from continuum.db.models.entity import Entity, EntityMember
from continuum.db.models.risk_file import RiskFileEvent
from continuum.resolution.cluster import ResolvedCluster

# Every table today that can hold a stale entity_id after a merge. Case and ContactEvent
# are both included even though Phase 1/5 populate them independently — a contact_event
# written against a since-absorbed singleton must move with it just like an account would.
ENTITY_FK_TABLES = (Account, ContactEvent, RiskFileEvent, Case)


def apply_resolution(
    session: Session, clusters: list[ResolvedCluster], resolution_run_id: uuid.UUID
) -> int:
    """Applies one resolution run's clusters to the database. Returns the number of
    singleton entities absorbed (merged away) in this run.

    A cluster of exactly one record is left untouched — it keeps whatever entity_id it
    already has (its Phase-1 singleton, or a prior run's survivor id) rather than being
    rewritten to the stable cluster id every run. Only an actual merge (a cluster spanning
    more than one still-distinct entity) mints the stable cluster id as a new survivor and
    absorbs the originals into it. Without this distinction, every unmerged record would
    get a freshly-minted entity_id on every run, breaking I6's "re-runnable without
    corrupting state" for every foreign key pointing at it.
    """
    absorbed_count = 0

    for cluster in clusters:
        member_entity_ids = _current_entity_ids_for_records(session, cluster.record_ids)

        if len(member_entity_ids) <= 1:
            continue  # already a single entity — nothing to merge, this run or last run

        survivor_id = cluster.entity_id
        absorbed_ids = member_entity_ids - {survivor_id}

        _upsert_survivor_entity(session, survivor_id, cluster, resolution_run_id)

        session.execute(
            update(EntityMember)
            .where(EntityMember.record_id.in_(cluster.record_ids))
            .values(entity_id=survivor_id)
        )

        for absorbed_id in absorbed_ids:
            for table in ENTITY_FK_TABLES:
                session.execute(
                    update(table).where(table.entity_id == absorbed_id).values(entity_id=survivor_id)
                )
            absorbed_entity = session.get(Entity, absorbed_id)
            if absorbed_entity is not None:
                session.delete(absorbed_entity)
                absorbed_count += 1

    return absorbed_count


def _current_entity_ids_for_records(
    session: Session, record_ids: tuple[uuid.UUID, ...]
) -> set[uuid.UUID]:
    rows = session.execute(
        select(EntityMember.entity_id).where(EntityMember.record_id.in_(record_ids))
    ).scalars()
    return set(rows)


def _upsert_survivor_entity(
    session: Session, survivor_id: uuid.UUID, cluster: ResolvedCluster, resolution_run_id: uuid.UUID
) -> None:
    existing = session.get(Entity, survivor_id)
    if existing is None:
        session.add(
            Entity(
                entity_id=survivor_id,
                cluster_size=len(cluster.record_ids),
                confidence=cluster.confidence,
                resolution_run_id=resolution_run_id,
            )
        )
    else:
        existing.cluster_size = len(cluster.record_ids)
        existing.confidence = cluster.confidence
        existing.resolution_run_id = resolution_run_id
