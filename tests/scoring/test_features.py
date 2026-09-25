"""Episode-frame construction on small hand-built tables: definitions and, above all, no
leakage from after the decision date (CLAUDE.md §8 "proper time separation")."""

from __future__ import annotations

import uuid
from datetime import date

import pandas as pd
import pytest

from continuum.db.models.account import DelinquencyBucket
from continuum.scoring.features import FEATURES, build_episode_frame

AS_OF = date(2026, 1, 1)
A1, A2 = uuid.uuid4(), uuid.uuid4()
E1 = uuid.uuid4()


def _tables(
    cure_paid: date | None = date(2025, 5, 1),
    contact_day: date | None = date(2025, 4, 1),
    extra_late_payment: bool = False,
):
    """Account A1: monthly 100 installments from 2025-01-01. Feb/Mar paid a few days late,
    the 2025-03-01 installment goes unpaid -> 30 DPD on 2025-03-31 (decision date)."""
    accounts = pd.DataFrame(
        {
            "account_id": [A1, A2],
            "entity_id": [E1, E1],
            "product": ["personal_loan", "auto_loan"],
            "exposure": [1200.0, 5000.0],
            "origination_date": [date(2025, 1, 1), date(2024, 6, 1)],
        }
    )
    rows = [
        (A1, date(2025, 1, 1), date(2025, 1, 1), 100.0, 100.0),
        (A1, date(2025, 2, 1), date(2025, 2, 5), 100.0, 100.0),  # 4 days late
        (A1, date(2025, 3, 1), cure_paid, 100.0, 100.0 if cure_paid else 0.0),
        (A1, date(2025, 4, 1), cure_paid, 100.0, 100.0 if cure_paid else 0.0),
    ]
    if extra_late_payment:
        rows.append((A1, date(2025, 5, 1), date(2025, 12, 30), 100.0, 100.0))
    payments = pd.DataFrame(
        rows, columns=["account_id", "due_date", "paid_date", "amount_due", "amount_paid"]
    )
    states = pd.DataFrame(
        {
            "account_id": [A1, A1, A2],
            "effective_from": [date(2025, 1, 1), date(2025, 3, 31), date(2025, 3, 1)],
            "state": [
                DelinquencyBucket.CURRENT,
                DelinquencyBucket.DPD_30,
                DelinquencyBucket.DPD_60,
            ],
        }
    )
    contacts = pd.DataFrame(
        {
            "account_id": [A1] if contact_day else [],
            "sent_at": [pd.Timestamp(contact_day) + pd.Timedelta(hours=10)] if contact_day else [],
        }
    )
    return accounts, payments, states, contacts


def _frame(**kw):
    return build_episode_frame(*_tables(**kw), as_of=AS_OF)


def _row(frame):
    return frame[frame["account_id"] == A1].iloc[0]


class TestDefinitions:
    def test_one_row_per_episode_with_decision_date_at_first_30_dpd(self) -> None:
        frame = _frame()
        assert set(frame["account_id"]) == {A1}  # A2 never had a DPD_30 state -> no episode
        assert _row(frame)["decision_date"] == date(2025, 3, 31)

    def test_overdue_amount_counts_installments_due_and_unpaid_on_the_decision_date(self) -> None:
        row = _row(_frame())
        assert row["overdue_amount"] == 100.0  # only 2025-03-01; the 04-01 one is not due yet
        assert row["n_overdue_installments"] == 1

    def test_cure_inside_window_is_cured_and_recovered(self) -> None:
        row = _row(_frame(cure_paid=date(2025, 5, 1)))  # 31 days after decision
        assert bool(row["cured_60"]) is True and row["recovered_amount"] == 100.0

    def test_cure_after_window_is_not_cured(self) -> None:
        row = _row(_frame(cure_paid=date(2025, 8, 1)))  # > 60 days
        assert bool(row["cured_60"]) is False and row["recovered_amount"] == 0.0

    def test_never_paid_is_not_cured(self) -> None:
        assert bool(_row(_frame(cure_paid=None))["cured_60"]) is False

    def test_treatment_is_a_contact_on_the_decision_date_only(self) -> None:
        assert bool(_row(_frame(contact_day=date(2025, 3, 31)))["treated"]) is True
        assert bool(_row(_frame(contact_day=date(2025, 4, 5)))["treated"]) is False
        assert bool(_row(_frame(contact_day=None))["treated"]) is False

    def test_prior_lateness_features(self) -> None:
        row = _row(_frame())
        assert row["n_prior_installments"] == 2
        assert row["prior_late_rate"] == pytest.approx(0.5)
        assert row["prior_max_days_late"] == 4

    def test_entity_features_see_the_sibling_account(self) -> None:
        row = _row(_frame())
        assert row["entity_n_accounts"] == 2
        assert row["entity_n_other_delinquent"] == 1  # A2 is 60 DPD on the decision date

    def test_window_still_open_is_unlabelled(self) -> None:
        accounts, payments, states, contacts = _tables()
        frame = build_episode_frame(accounts, payments, states, contacts, as_of=date(2025, 4, 15))
        assert not bool(_row(frame)["labelled"])


class TestNoLeakageFromAfterTheDecisionDate:
    def test_features_are_unchanged_by_post_decision_payments(self) -> None:
        base = _row(_frame(cure_paid=date(2025, 5, 1)))
        other = _row(_frame(cure_paid=date(2025, 8, 1), extra_late_payment=True))
        for feature in FEATURES:
            assert base[feature] == other[feature], feature

    def test_features_are_unchanged_by_contacts_after_the_decision_date(self) -> None:
        base = _row(_frame(contact_day=None))
        late_contact = _row(_frame(contact_day=date(2025, 6, 1)))
        for feature in FEATURES:
            assert base[feature] == late_contact[feature], feature

    def test_label_columns_are_not_features(self) -> None:
        assert not {"treated", "cured_60", "recovered_amount", "labelled"} & set(FEATURES)
