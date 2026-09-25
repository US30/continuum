"""Phase 2 entrypoint: blocking -> Splink model -> stable clustering -> merge -> network.

Run via `make resolve` (`python -m continuum.resolution.run`). Reads every party_record,
predicts pairwise matches, clusters them, merges the resulting clusters into the entity
table via continuum.resolution.merge (repointing Phase-1 singletons), and materialises the
one-hop network via continuum.resolution.network. Prints a summary — cluster-size
distribution and edges written — but never touches ground_truth_link (CLAUDE.md I4): that
comparison is eval.resolution_metrics's job, run separately via `make resolve-metrics`.
"""

from __future__ import annotations

import uuid
from collections import Counter

import pandas as pd
import typer
from sqlalchemy import select

from continuum.config.settings import get_settings
from continuum.db.models.party import PartyRecord
from continuum.db.session import get_engine, session_scope
from continuum.resolution import merge, network
from continuum.resolution.cluster import (
    MATCH_PROBABILITY_THRESHOLD,
    ResolvedCluster,
    clusters_from_predictions,
)
from continuum.resolution.model import build_linker
from continuum.telemetry.logging import configure_logging, get_logger

app = typer.Typer(add_completion=False)
logger = get_logger(__name__)

PARTY_RECORD_COLUMNS = (
    "record_id",
    "full_name",
    "date_of_birth",
    "national_id",
    "address_line",
    "postal_code",
    "phone",
)


def _load_party_records() -> pd.DataFrame:
    query = select(*(getattr(PartyRecord, col) for col in PARTY_RECORD_COLUMNS))
    return pd.read_sql(query, get_engine())


def resolve() -> tuple[int, int, int]:
    """Runs one full resolution pass. Returns (n_records, n_clusters, n_merged)."""
    records = _load_party_records()
    records["record_id"] = records["record_id"].astype(str)

    linker = build_linker(records)
    predictions = linker.inference.predict()
    clustered = linker.clustering.cluster_pairwise_predictions_at_threshold(
        predictions, threshold_match_probability=MATCH_PROBABILITY_THRESHOLD
    )
    cluster_df = clustered.as_pandas_dataframe()  # type: ignore[no-untyped-call]
    cluster_rows = cluster_df.to_dict("records")
    clusters = clusters_from_predictions(cluster_rows)

    resolution_run_id = uuid.uuid4()
    with session_scope() as session:
        n_merged = merge.apply_resolution(session, clusters, resolution_run_id)
        n_edges = network.materialise_network(session)

    _print_summary(len(records), clusters, n_merged, n_edges, resolution_run_id)
    return len(records), len(clusters), n_merged


def _print_summary(
    n_records: int,
    clusters: list[ResolvedCluster],
    n_merged: int,
    n_edges: int,
    resolution_run_id: uuid.UUID,
) -> None:
    size_counts = Counter(len(c.record_ids) for c in clusters)

    typer.echo("")
    typer.echo("=== CONTINUUM resolution summary ===")
    typer.echo(f"resolution_run_id:      {resolution_run_id}")
    typer.echo(f"party_records seen:     {n_records}")
    typer.echo(f"clusters:               {len(clusters)}")
    for size, count in sorted(size_counts.items()):
        typer.echo(f"  size {size:<4} clusters   {count}")
    typer.echo(f"singleton entities merged away: {n_merged}")
    typer.echo(f"network edges written:  {n_edges}")
    typer.echo("")
    typer.echo("Precision/recall/F1 against ground_truth_link: run `make resolve-metrics`.")
    typer.echo("")


@app.command()
def main() -> None:
    configure_logging(get_settings().log_level)
    logger.info("resolution.start")
    resolve()


if __name__ == "__main__":
    app()
