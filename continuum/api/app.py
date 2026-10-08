"""The review console: the queue of cases parked for a human, and the decision that resumes one.

    GET  /healthz                      liveness (no dependencies)
    GET  /reviews                      the queue, largest exposure first
    GET  /reviews/{entity_id}          one parked case: guard reasons, draft, accounts, network
    POST /reviews/{entity_id}/decision approve | reject | acknowledge
    GET  /api/reviews                  the queue as JSON

This service never runs the graph and never sends anything. A decision is *claimed* (taken off
the queue, so it can be made only once) and handed to the worker as `resume_case_job`; the
worker resumes the parked case, and any send still goes through the guard's dispatch (I2). A
hard-blocked case can only be acknowledged — the console refuses `approve` for it, and the
route node would ignore it anyway.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from continuum.api.casefile import CaseFile, load_case_file
from continuum.api.identity import reviewer_identity
from continuum.config.settings import get_settings
from continuum.db.session import session_scope
from continuum.telemetry.logging import configure_logging, get_logger, hash_pii
from continuum.workers import review_queue

logger = get_logger(__name__)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

Decision = Literal["approve", "reject", "acknowledge"]


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.redis = await create_pool(
        RedisSettings(host=settings.redis_host, port=settings.redis_port)
    )
    try:
        yield
    finally:
        await app.state.redis.aclose()


def get_redis(request: Request) -> Any:
    """The Arq pool: both the review-queue hash and `enqueue_job`. Overridden in tests."""
    return request.app.state.redis


def _case_file(entity_id: uuid.UUID) -> CaseFile:
    with session_scope() as session:
        return load_case_file(session, entity_id)


def _require_same_origin(request: Request) -> None:
    """Refuse cross-site form posts. The reviewer is authenticated by a proxy cookie, so a
    page on another site could otherwise submit a decision in their name."""
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        raise HTTPException(status_code=403, detail="cross-site request refused")


Redis = Annotated[Any, Depends(get_redis)]
Reviewer = Annotated[str, Depends(reviewer_identity)]


def create_app() -> FastAPI:
    app = FastAPI(title="CONTINUUM review console", lifespan=_lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse("/reviews", status_code=303)

    @app.get("/api/reviews")
    async def reviews_json(redis: Redis, reviewer: Reviewer) -> list[dict[str, Any]]:
        return await review_queue.list_pending(redis)

    @app.get("/reviews", response_class=HTMLResponse)
    async def reviews(request: Request, redis: Redis, reviewer: Reviewer) -> HTMLResponse:
        pending = await review_queue.list_pending(redis)
        return templates.TemplateResponse(
            request, "queue.html", {"reviews": pending, "reviewer": reviewer}
        )

    @app.get("/reviews/{entity_id}", response_class=HTMLResponse)
    async def review(
        request: Request, entity_id: uuid.UUID, redis: Redis, reviewer: Reviewer
    ) -> HTMLResponse:
        pending = await review_queue.get_pending(redis, entity_id)
        if pending is None:
            raise HTTPException(status_code=404, detail="no review waiting for this entity")
        case_file = await run_in_threadpool(_case_file, entity_id)
        return templates.TemplateResponse(
            request, "review.html", {"review": pending, "file": case_file, "reviewer": reviewer}
        )

    @app.post("/reviews/{entity_id}/decision")
    async def decide(
        request: Request,
        entity_id: uuid.UUID,
        decision: Annotated[Decision, Form()],
        redis: Redis,
        reviewer: Reviewer,
    ) -> RedirectResponse:
        _require_same_origin(request)
        pending = await review_queue.get_pending(redis, entity_id)
        if pending is None:
            raise HTTPException(status_code=409, detail="this review was already decided")
        if decision == "approve" and not pending.get("may_approve"):
            raise HTTPException(status_code=400, detail="a hard block cannot be approved")
        claimed = await review_queue.claim(redis, entity_id)
        if claimed is None:
            raise HTTPException(status_code=409, detail="this review was already decided")
        await redis.enqueue_job(
            "resume_case_job", str(entity_id), decision, reviewer, json.dumps(claimed)
        )
        logger.info(
            "review.decided",
            entity_hash=hash_pii(str(entity_id)),
            decision=decision,
            reviewer_hash=hash_pii(reviewer),
        )
        return RedirectResponse("/reviews", status_code=303)

    return app
