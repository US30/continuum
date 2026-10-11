"""Dashboard page: rendering with the database read stubbed, the metrics report loader, and
one integration test of the real aggregate queries."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from continuum.api import app as app_module
from continuum.api.dashboard import (
    CaseRow,
    Count,
    Dashboard,
    MetricBlock,
    load_dashboard,
    load_metrics,
)
from continuum.db.models.account import Account, DelinquencyBucket
from continuum.db.models.collections import Case
from continuum.db.models.entity import Entity
from continuum.db.session import session_scope
from tests.fakes import FakeRedis

ENTITY = uuid.UUID("00000000-0000-0000-0000-0000000000d1")


def _dashboard(metrics: list[MetricBlock] | None) -> Dashboard:
    return Dashboard(
        totals=[Count(label="Resolved entities", n=1234)],
        last_resolution_at=None,
        last_screening_at=None,
        account_states=[],
        edge_types=[Count(label="shared_phone", n=7)],
        screening=[],
        case_dispositions=[Count(label="open", n=3)],
        collapsed_cases=2,
        top_cases=[
            CaseRow(
                case_id=uuid.uuid4(),
                entity_id=ENTITY,
                rank=1,
                expected_recovery=412.5,
                disposition="open",
                delinquent_accounts=2,
                exposure=6000.0,
            )
        ],
        runtime_contacts=[],
        simulated_contacts=0,
        risk_file_events=[],
        metrics=metrics,
    )


def _client(monkeypatch: pytest.MonkeyPatch, data: Dashboard) -> TestClient:
    monkeypatch.setattr(app_module, "_dashboard", lambda: data)
    app = app_module.create_app()
    app.dependency_overrides[app_module.get_redis] = lambda: FakeRedis()
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    block = MetricBlock(title="Metric 3 · entity view vs. account view", values={"n_cases": 713})
    yield _client(monkeypatch, _dashboard([block]))


def test_page_shows_counts_cases_and_metrics(client: TestClient) -> None:
    page = client.get("/dashboard").text
    assert "1,234" in page and "shared_phone" in page
    assert "<strong>2</strong> cases" in page and str(ENTITY)[:8] in page and "412.50" in page
    assert "Metric 3" in page and "713" in page
    assert "0 waiting for review" in page


def test_json_carries_the_same_data(client: TestClient) -> None:
    body = client.get("/api/dashboard").json()
    assert body["collapsed_cases"] == 2 and body["top_cases"][0]["entity_id"] == str(ENTITY)


def test_missing_report_is_shown_as_not_computed(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _client(monkeypatch, _dashboard(None)).get("/dashboard").text
    assert "Not computed" in page and "Metric 3" not in page


def test_empty_sections_say_so(client: TestClient) -> None:
    page = client.get("/dashboard").text
    assert "No screening decisions yet." in page and "Nothing has been dispatched." in page


def test_load_metrics_without_a_report_is_none(tmp_path: Path) -> None:
    assert load_metrics(tmp_path / "metrics_report.json") is None


def test_load_metrics_keeps_headline_scalars_only(tmp_path: Path) -> None:
    report = tmp_path / "metrics_report.json"
    report.write_text(
        json.dumps(
            {
                "config": {"data_seed": 1},
                "metric1_screening": {"labelled_pairs": 18, "missed_true_matches": []},
                "metric3_network": {"validated_share": 0.05},
                "_dataclasses": {"audit": {"sampled": 263}},
            }
        )
    )
    blocks = load_metrics(report)
    assert blocks is not None
    assert [b.values for b in blocks] == [{"labelled_pairs": 18}, {"validated_share": 0.05}]
    assert blocks[0].title.startswith("Metric 1")


@pytest.mark.integration
def test_load_dashboard_counts_a_collapsed_case(tmp_path: Path) -> None:
    entity_id = uuid.uuid4()
    missing_report = tmp_path / "metrics_report.json"
    with session_scope() as session:
        before = load_dashboard(session, missing_report)
        session.add(
            Entity(
                entity_id=entity_id,
                cluster_size=1,
                confidence=Decimal("1.0"),
                resolution_run_id=uuid.uuid4(),
            )
        )
        session.flush()
        for state, exposure in (
            (DelinquencyBucket.DPD_30, "2000.00"),
            (DelinquencyBucket.DPD_60, "4000.00"),
            (DelinquencyBucket.CURRENT, "9000.00"),
        ):
            session.add(
                Account(
                    account_id=uuid.uuid4(),
                    entity_id=entity_id,
                    product="loan",
                    exposure=Decimal(exposure),
                    origination_date=date(2025, 1, 1),
                    current_state=state,
                )
            )
        # Rank below any real rank, so this case leads the list whatever else is in the table.
        session.add(Case(case_id=uuid.uuid4(), entity_id=entity_id, rank=-1))
        session.flush()

        after = load_dashboard(session, missing_report)

    assert after.collapsed_cases == before.collapsed_cases + 1
    top = after.top_cases[0]
    assert (top.entity_id, top.delinquent_accounts, top.exposure) == (entity_id, 2, 6000.0)
    assert after.metrics is None
    totals = {t.label: t.n for t in after.totals}
    assert totals["Accounts"] == {t.label: t.n for t in before.totals}["Accounts"] + 3
