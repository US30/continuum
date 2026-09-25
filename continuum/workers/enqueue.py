"""Enqueue a batch of cases on Arq and wait for the worker to finish them.

    python -m continuum.workers.enqueue --limit 20

Start the worker first:  arq continuum.workers.tasks.WorkerSettings
Cases the guard escalates or blocks come back as `interrupted`: parked in the checkpointer for
a human, resumable with continuum.graph.run.resume_case.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from typing import Any

import typer
from arq import create_pool
from arq.jobs import Job

from continuum.config.settings import get_settings
from continuum.db.session import session_scope
from continuum.graph.queue import pick_entities, rank_cases
from continuum.workers.tasks import _redis_settings

app = typer.Typer(add_completion=False)


async def _run(entities: list[uuid.UUID], timeout: float) -> list[dict[str, Any]]:
    pool = await create_pool(_redis_settings())
    jobs: list[Job] = []
    for entity_id in entities:
        job = await pool.enqueue_job("run_case_job", str(entity_id))
        assert job is not None
        jobs.append(job)

    results: list[dict[str, Any]] = []
    for job in jobs:
        try:
            results.append(await job.result(timeout=timeout))
        except Exception as exc:
            status = await job.status()
            results.append(
                {"status": "error", "error": f"{type(exc).__name__}: {exc}", "job": status}
            )
    await pool.close()
    return results


@app.command()
def main(
    limit: int = typer.Option(20, help="Number of entities to run."),
    timeout: float = typer.Option(300.0, help="Seconds to wait per job."),
) -> None:
    get_settings()
    with session_scope() as session:
        picks = pick_entities(session, limit)
    typer.echo(f"enqueueing {len(picks)} cases")
    results = asyncio.run(_run([p.entity_id for p in picks], timeout))

    typer.echo("")
    typer.echo(f"{'sample':<28}{'status':<13}{'route / guard':<34}{'net recovery':>13}")
    outcomes: Counter[str] = Counter()
    for pick, result in zip(picks, results, strict=True):
        if result["status"] == "done":
            detail = f"{result['route']} / {result['guard']}"
            net = result.get("expected_net_recovery")
            net_text = f"{net:>13.2f}" if net is not None else f"{'-':>13}"
        elif result["status"] == "interrupted":
            detail = f"needs human ({result['review']['verdict']})"
            net_text = f"{'-':>13}"
        else:
            detail, net_text = result.get("error", "error")[:32], f"{'-':>13}"
        outcomes[result["status"] if result["status"] != "done" else result["route"]] += 1
        typer.echo(f"{pick.why:<28}{result['status']:<13}{detail:<34}{net_text}")

    with session_scope() as session:
        n_ranked = rank_cases(session)
    typer.echo("")
    typer.echo(f"outcomes: {dict(outcomes)}   cases ranked: {n_ranked}")


if __name__ == "__main__":
    app()
