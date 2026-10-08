"""The nightly batch as one Prefect flow (CLAUDE.md §7).

    foundation.refresh   loan state + sanctions snapshot
      -> resolution.run  blocking -> Splink match -> cluster -> materialise network
        -> screening     select (delinquency change in 24h + never screened) -> candidates
                         -> auto_confirm / auto_reject / adjudicate the ambiguous band
        -> audit.sample  random slice of auto-cleared -> loose re-screen (suspect rate)
        -> scoring.train refit the uplift model on tonight's book for the case workers

Each stage is the same function the `make` targets run, wrapped as a Prefect task; this module
adds only the order. Stages run strictly one after another and the flow stops at the first
failure. Every stage is idempotent (I6), so a failed night is re-run from the top.

`scoring.train` is not in §7's diagram: it is here because the case workers need a model
artifact that matches the night's data, and nothing else in the cloud produces one.

Run with `python -m continuum.flows.nightly` (the Cloud Run job's command). No Prefect server
is required: without PREFECT_API_URL the flow runs against Prefect's in-process ephemeral API.
"""

from __future__ import annotations

from typing import Any

import typer
from prefect import flow, task

from continuum.config.settings import get_settings
from continuum.db.session import session_scope
from continuum.foundation import build as foundation_build
from continuum.resolution import run as resolution_run
from continuum.scoring import train as scoring_train
from continuum.screening import audit as screening_audit
from continuum.screening import run as screening_run
from continuum.telemetry.logging import configure_logging, get_logger, set_correlation_id

app = typer.Typer(add_completion=False)
logger = get_logger(__name__)


@task(name="foundation.refresh")
def foundation_refresh(n_persons: int) -> None:
    foundation_build.build(n_persons=n_persons, seed=None)


@task(name="resolution.run")
def resolve_entities() -> dict[str, int]:
    n_records, n_clusters, n_merged = resolution_run.resolve()
    return {"records": n_records, "clusters": n_clusters, "merged": n_merged}


@task(name="screening")
def screen(full: bool) -> None:
    settings = get_settings()
    screening_run.run(
        adjudicate_enabled=settings.llm_enabled,
        full=full,
        max_adjudications=settings.screening_max_adjudications_per_run,
    )


@task(name="audit.sample")
def audit_sample() -> dict[str, Any]:
    with session_scope() as session:
        sample = screening_audit.sample_audit(session, get_settings().data_as_of_date)
    low, high = sample.suspect_rate_ci
    result = {
        "population": sample.population,
        "sampled": len(sample.sampled),
        "suspects": len(sample.suspects),
        "suspect_rate": round(sample.suspect_rate, 4),
        "suspect_rate_ci": [round(low, 4), round(high, 4)],
    }
    logger.info("audit.sample", **result)
    return result


@task(name="scoring.train")
def train_scoring_model() -> str:
    as_of = get_settings().data_as_of_date
    model, split, _ = scoring_train.train_from_database(as_of)
    scoring_train.save_artifact(model, split, as_of)
    return str(model.version)


@flow(name="nightly-batch")
def nightly_batch(
    n_persons: int = foundation_build.DEFAULT_N_PERSONS, full_screen: bool = False
) -> dict[str, Any]:
    as_of = get_settings().data_as_of_date
    set_correlation_id(f"nightly-{as_of.isoformat()}")
    foundation_refresh(n_persons)
    resolution = resolve_entities()
    screen(full_screen)
    audit = audit_sample()
    model_version = train_scoring_model()
    summary = {"resolution": resolution, "audit": audit, "model_version": model_version}
    logger.info("nightly.done", **summary)
    return summary


@app.command()
def main(
    n_persons: int = typer.Option(
        foundation_build.DEFAULT_N_PERSONS, help="Size of the synthetic book to (re)build."
    ),
    full_screen: bool = typer.Option(
        False, help="Screen every entity, not just new/changed ones (sweeps up deferred work)."
    ),
) -> None:
    configure_logging(get_settings().log_level)
    nightly_batch(n_persons=n_persons, full_screen=full_screen)


if __name__ == "__main__":
    app()
