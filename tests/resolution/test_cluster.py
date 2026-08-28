"""Tests for the pure stable-clustering logic (no Splink, no DB).

CLAUDE.md §8 done-when: "cluster ids are stable across two consecutive runs on unchanged
data" — the property these tests exist to lock in.
"""

from __future__ import annotations

import uuid

from continuum.resolution.cluster import clusters_from_predictions, stable_cluster_id

A, B, C, D = (uuid.uuid4() for _ in range(4))


class TestStableClusterId:
    def test_deterministic_given_same_members(self) -> None:
        assert stable_cluster_id([A, B]) == stable_cluster_id([A, B])

    def test_order_independent(self) -> None:
        assert stable_cluster_id([A, B, C]) == stable_cluster_id([C, A, B])

    def test_different_membership_gives_different_id(self) -> None:
        assert stable_cluster_id([A, B]) != stable_cluster_id([A, C])

    def test_singleton_id_differs_from_its_own_record_id(self) -> None:
        # Guards against an implementation that accidentally passes the record id through
        # unchanged for singleton "clusters" of one.
        assert stable_cluster_id([A]) != A


class TestClustersFromPredictions:
    def test_groups_by_splink_cluster_id(self) -> None:
        rows = [
            {"record_id": str(A), "cluster_id": "x"},
            {"record_id": str(B), "cluster_id": "x"},
            {"record_id": str(C), "cluster_id": "y"},
        ]
        clusters = clusters_from_predictions(rows)
        by_size = sorted(clusters, key=lambda c: len(c.record_ids))
        assert len(by_size[0].record_ids) == 1
        assert len(by_size[1].record_ids) == 2
        assert set(by_size[1].record_ids) == {A, B}

    def test_stable_id_matches_direct_computation(self) -> None:
        rows = [
            {"record_id": str(A), "cluster_id": "any-label"},
            {"record_id": str(B), "cluster_id": "any-label"},
        ]
        clusters = clusters_from_predictions(rows)
        assert clusters[0].entity_id == stable_cluster_id([A, B])

    def test_singleton_cluster_has_full_confidence(self) -> None:
        rows = [{"record_id": str(D), "cluster_id": "solo"}]
        clusters = clusters_from_predictions(rows)
        assert clusters[0].confidence == 1.0

    def test_reordering_input_rows_does_not_change_output_ids(self) -> None:
        rows = [
            {"record_id": str(A), "cluster_id": "x"},
            {"record_id": str(B), "cluster_id": "x"},
        ]
        shuffled = list(reversed(rows))
        ids_a = {c.entity_id for c in clusters_from_predictions(rows)}
        ids_b = {c.entity_id for c in clusters_from_predictions(shuffled)}
        assert ids_a == ids_b
