"""Review console: queue, case page, and the decision that hands a parked case to the worker.
Redis is faked and the database read is stubbed, so these are unit tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from continuum.api import app as app_module
from continuum.api.casefile import AccountRow, CaseFile, LinkRow
from continuum.config.settings import Settings
from continuum.workers import review_queue
from tests.fakes import FakeRedis

ESCALATED = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
BLOCKED = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
IAP = {"x-goog-authenticated-user-email": "accounts.google.com:pat@example.com"}


def _review(entity_id: uuid.UUID, verdict: str, **extra: Any) -> dict[str, Any]:
    return {
        "entity_id": str(entity_id),
        "case_id": str(uuid.uuid4()),
        "verdict": verdict,
        "reasons": ["total overdue above the review threshold"],
        "total_overdue": 6000.0,
        "draft": "Hi Sam, please get in touch.",
        "may_approve": verdict == "escalate",
        **extra,
    }


@pytest.fixture
def redis() -> FakeRedis:
    fake = FakeRedis()
    asyncio.run(review_queue.add_pending(fake, ESCALATED, _review(ESCALATED, "escalate")))
    asyncio.run(
        review_queue.add_pending(fake, BLOCKED, _review(BLOCKED, "block", total_overdue=900.0))
    )
    return fake


@pytest.fixture
def client(redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    def case_file(entity_id: uuid.UUID) -> CaseFile:
        return CaseFile(
            entity_id=entity_id,
            screening="clear",
            accounts=[
                AccountRow(account_id=uuid.uuid4(), product="card", state="60", exposure=4000.0),
                AccountRow(account_id=uuid.uuid4(), product="loan", state="30", exposure=2000.0),
            ],
            links=[LinkRow(entity_id=uuid.uuid4(), edge_type="shared_phone", strength=0.9)],
        )

    monkeypatch.setattr(app_module, "_case_file", case_file)
    app = app_module.create_app()
    app.dependency_overrides[app_module.get_redis] = lambda: redis
    # No lifespan (no `with`): the real Redis pool is never opened.
    yield TestClient(app, follow_redirects=False)


def test_healthz_needs_nothing(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_queue_lists_waiting_cases_largest_first(client: TestClient) -> None:
    listed = client.get("/api/reviews").json()
    assert [r["entity_id"] for r in listed] == [str(ESCALATED), str(BLOCKED)]
    page = client.get("/reviews").text
    assert "2 cases waiting" in page and str(ESCALATED)[:8] in page


def test_case_page_shows_every_account_and_the_network(client: TestClient) -> None:
    page = client.get(f"/reviews/{ESCALATED}").text
    assert "Accounts held by this entity (2)" in page
    assert "shared_phone" in page and "Approve and send" in page


def test_untrusted_text_is_escaped(client: TestClient, redis: FakeRedis) -> None:
    hostile = _review(ESCALATED, "escalate", draft="<script>alert(1)</script>")
    asyncio.run(review_queue.add_pending(redis, ESCALATED, hostile))
    page = client.get(f"/reviews/{ESCALATED}").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_a_hard_block_offers_no_approve_button(client: TestClient) -> None:
    page = client.get(f"/reviews/{BLOCKED}").text
    assert "Acknowledge block" in page and "Approve and send" not in page


def test_approving_hands_the_case_to_the_worker_with_the_reviewer(
    client: TestClient, redis: FakeRedis
) -> None:
    response = client.post(
        f"/reviews/{ESCALATED}/decision", data={"decision": "approve"}, headers=IAP
    )
    assert response.status_code == 303 and response.headers["location"] == "/reviews"
    (job,) = redis.jobs
    assert job[:4] == ("resume_case_job", str(ESCALATED), "approve", "pat@example.com")
    assert json.loads(job[4])["entity_id"] == str(ESCALATED)  # payload kept for re-queue on failure
    assert [r["entity_id"] for r in client.get("/api/reviews").json()] == [str(BLOCKED)]


def test_a_review_cannot_be_decided_twice(client: TestClient, redis: FakeRedis) -> None:
    first = client.post(f"/reviews/{ESCALATED}/decision", data={"decision": "reject"})
    second = client.post(f"/reviews/{ESCALATED}/decision", data={"decision": "approve"})
    assert (first.status_code, second.status_code) == (303, 409)
    assert len(redis.jobs) == 1


def test_a_hard_block_cannot_be_approved(client: TestClient, redis: FakeRedis) -> None:
    response = client.post(f"/reviews/{BLOCKED}/decision", data={"decision": "approve"})
    assert response.status_code == 400
    assert redis.jobs == [] and len(client.get("/api/reviews").json()) == 2  # still queued


def test_unknown_decision_is_rejected(client: TestClient, redis: FakeRedis) -> None:
    response = client.post(f"/reviews/{ESCALATED}/decision", data={"decision": "send_anyway"})
    assert response.status_code == 422 and redis.jobs == []


def test_cross_site_post_is_refused(client: TestClient, redis: FakeRedis) -> None:
    response = client.post(
        f"/reviews/{ESCALATED}/decision",
        data={"decision": "approve"},
        headers={"sec-fetch-site": "cross-site"},
    )
    assert response.status_code == 403 and redis.jobs == []


def test_missing_review_is_404(client: TestClient) -> None:
    assert client.get(f"/reviews/{uuid.uuid4()}").status_code == 404


def test_outside_local_a_request_without_proxy_identity_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from continuum.api import identity

    monkeypatch.setattr(identity, "get_settings", lambda: Settings(environment="production"))
    assert client.get("/reviews").status_code == 401
    assert client.get("/reviews", headers=IAP).status_code == 200
    assert client.get("/healthz").status_code == 200
