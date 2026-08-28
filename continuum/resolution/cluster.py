"""Pairwise match predictions -> stable entity clusters.

CLAUDE.md §8 done-when: "cluster ids are stable across two consecutive runs on unchanged
data." Splink's connected-components clustering assigns its own cluster label, which is not
guaranteed stable across runs (it depends on internal row order). This module remaps that
label to a deterministic id derived from the *sorted set* of member record_ids, so
re-running on unchanged input always yields the same entity_id per cluster regardless of
Splink's internal labelling or input row order — required for CLAUDE.md I6.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

# Not yet calibrated against a real run (CLAUDE.md §10) — Splink's own documentation
# suggests 0.5-0.95 as a starting band for dedupe_only depending on comparison strictness;
# picked at the strict end since I5 makes recall continuum.screening's job, not this one's:
# resolution merging two different people into one entity is the more expensive mistake
# here, because it would make collections treat unrelated people as one case (CLAUDE.md
# §2's precise failure mode in reverse). Revisit once eval.resolution_metrics has run once.
MATCH_PROBABILITY_THRESHOLD = 0.95

CLUSTER_ID_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-0000000000c1")


@dataclass(frozen=True)
class ResolvedCluster:
    entity_id: uuid.UUID
    record_ids: tuple[uuid.UUID, ...]  # sorted — the basis of the stable id below
    # 1.0 for an unmerged singleton (nothing to be uncertain about); otherwise the
    # clustering threshold itself, as a conservative floor — Splink's clustering step
    # collapses pairwise probabilities into connected components and does not surface a
    # single per-cluster confidence, so this is an approximation, not a measured value.
    confidence: float


def stable_cluster_id(record_ids: list[uuid.UUID]) -> uuid.UUID:
    """Deterministic id from the sorted set of member record_ids. Unaffected by Splink's
    row order or its own internal cluster labelling, and unaffected by which record
    happened to be the "canonical" one at Phase 1 load time.
    """
    key = ",".join(sorted(str(record_id) for record_id in record_ids))
    return uuid.uuid5(CLUSTER_ID_NAMESPACE, key)


def clusters_from_predictions(cluster_rows: list[dict[str, Any]]) -> list[ResolvedCluster]:
    """`cluster_rows` is the row-dict form of
    `linker.cluster_pairwise_predictions_at_threshold(...).as_pandas_dataframe()`
    (call `.to_dict("records")` on it before passing in here) — one row per record_id
    carrying Splink's own `cluster_id` (unstable, discarded) that this function regroups by.
    """
    groups: dict[Any, list[uuid.UUID]] = {}
    for row in cluster_rows:
        groups.setdefault(row["cluster_id"], []).append(uuid.UUID(str(row["record_id"])))

    resolved: list[ResolvedCluster] = []
    for members in groups.values():
        record_ids = tuple(sorted(set(members)))
        resolved.append(
            ResolvedCluster(
                entity_id=stable_cluster_id(list(record_ids)),
                record_ids=record_ids,
                confidence=1.0 if len(record_ids) == 1 else MATCH_PROBABILITY_THRESHOLD,
            )
        )
    return resolved
