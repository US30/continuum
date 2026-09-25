"""Episode dataset for the self-cure / response / uplift models.

One row per delinquency episode: an account on the day it first reaches 30 DPD (its
*decision date*), with

  - features known **at** that date (nothing from later — see the leakage rules below),
  - treatment: was the account contacted on the decision date (the historical rules policy),
  - outcome: did it cure — pay every installment overdue on the decision date — within
    CURE_WINDOW_DAYS, and the currency recovered if so,
  - `labelled`: whether the outcome window closed on or before the data's as-of date. Episodes
    whose window is still open are unlabelled and must not be trained or scored on.

Leakage rules (CLAUDE.md §8 "proper time separation"): a payment counts as known at the
decision date only if its paid_date is on or before it; the contact is read only for the
decision date itself; entity-level features use accounts that existed on the decision date
and their delinquency state as of that date. Outcomes are the only thing computed from
after the decision date, and they are never features. tests/scoring/test_features.py
mutates post-decision data and checks no feature moves.

Pure functions over dataframes (no I/O) so they are exhaustively testable; `load_episode_frame`
is the thin database wrapper.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from continuum.db.models.account import Account, DelinquencyBucket, DelinquencyState, PaymentEvent
from continuum.db.models.collections import ContactEvent
from continuum.foundation.causal import CURE_WINDOW_DAYS

DELINQUENT_STATES = {DelinquencyBucket.DPD_30, DelinquencyBucket.DPD_60, DelinquencyBucket.DPD_90}

PRODUCTS = ("personal_loan", "auto_loan", "credit_card")

NUMERIC_FEATURES = [
    "log_exposure",
    "months_on_book",
    "log_overdue_amount",
    "n_overdue_installments",
    "n_prior_installments",
    "prior_late_rate",
    "prior_mean_days_late",
    "prior_max_days_late",
    "entity_n_accounts",
    "entity_log_total_exposure",
    "entity_n_other_delinquent",
]
PRODUCT_FEATURES = [f"product_{p}" for p in PRODUCTS]
FEATURES = NUMERIC_FEATURES + PRODUCT_FEATURES

ID_COLUMNS = ["account_id", "entity_id", "decision_date"]
LABEL_COLUMNS = ["treated", "cured_60", "recovered_amount", "overdue_amount", "labelled"]


def _to_ts(values: pd.Series) -> pd.Series:
    """Normalise dates / timestamps (naive or tz-aware) to naive midnight timestamps."""
    return pd.to_datetime(values, utc=True).dt.tz_localize(None).dt.normalize()


def _day(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values).dt.normalize()


def build_episode_frame(
    accounts: pd.DataFrame,
    payments: pd.DataFrame,
    states: pd.DataFrame,
    contacts: pd.DataFrame,
    as_of: date,
) -> pd.DataFrame:
    """Inputs (columns):
    accounts  account_id, entity_id, product, exposure, origination_date
    payments  account_id, due_date, paid_date, amount_due, amount_paid
    states    account_id, effective_from, state
    contacts  account_id, sent_at
    """
    as_of_ts = pd.Timestamp(as_of)

    st = states.assign(effective_from=_to_ts(states["effective_from"]))
    first_30 = (
        st[st["state"] == DelinquencyBucket.DPD_30]
        .groupby("account_id", as_index=False)["effective_from"]
        .min()
        .rename(columns={"effective_from": "decision_date"})
    )
    ep = first_30.merge(
        accounts.assign(origination_date=_day(accounts["origination_date"])),
        on="account_id",
        how="inner",
    )

    pay = payments.assign(
        due_date=_day(payments["due_date"]),
        paid_date=pd.to_datetime(payments["paid_date"]),
        amount_due=payments["amount_due"].astype(float),
        amount_paid=payments["amount_paid"].astype(float),
    )
    pe = pay.merge(ep[["account_id", "decision_date"]], on="account_id", how="inner")

    # Paid in full by a date <=> paid_date not null and on/before it (partials never count).
    fully_paid = pe["amount_paid"] >= pe["amount_due"]
    paid_by_decision = (
        fully_paid & pe["paid_date"].notna() & (pe["paid_date"] <= pe["decision_date"])
    )
    overdue = pe[(pe["due_date"] <= pe["decision_date"]) & ~paid_by_decision].copy()

    window_end = overdue["decision_date"] + pd.Timedelta(days=CURE_WINDOW_DAYS)
    overdue["paid_in_window"] = (
        (overdue["amount_paid"] >= overdue["amount_due"])
        & overdue["paid_date"].notna()
        & (overdue["paid_date"] <= window_end)
    )
    per_ep = overdue.groupby("account_id").agg(
        overdue_amount=("amount_due", "sum"),
        n_overdue_installments=("amount_due", "size"),
        first_overdue_due=("due_date", "min"),
        cured_60=("paid_in_window", "all"),
    )
    # Currency recovered inside the window: overdue installments paid in full by then.
    per_ep["recovered_amount"] = (
        overdue[overdue["paid_in_window"]].groupby("account_id")["amount_due"].sum()
    )
    per_ep["recovered_amount"] = per_ep["recovered_amount"].fillna(0.0)
    per_ep = per_ep.reset_index()
    ep = ep.merge(per_ep, on="account_id", how="inner")

    # ---- prior payment behaviour: installments due before the first overdue one, paid by D
    pe = pe.merge(ep[["account_id", "first_overdue_due"]], on="account_id", how="inner")
    prior = pe[
        (pe["due_date"] < pe["first_overdue_due"])
        & pe["paid_date"].notna()
        & (pe["paid_date"] <= pe["decision_date"])
    ].copy()
    prior["days_late"] = (prior["paid_date"] - prior["due_date"]).dt.days.clip(lower=0)
    prior_stats = prior.groupby("account_id").agg(
        n_prior_installments=("days_late", "size"),
        prior_late_rate=("days_late", lambda s: float((s > 0).mean())),
        prior_mean_days_late=("days_late", "mean"),
        prior_max_days_late=("days_late", "max"),
    )
    ep = ep.merge(prior_stats.reset_index(), on="account_id", how="left")
    for col in (
        "n_prior_installments",
        "prior_late_rate",
        "prior_mean_days_late",
        "prior_max_days_late",
    ):
        ep[col] = ep[col].fillna(0.0)

    # ---- treatment: contacted on the decision date itself
    ct = contacts.assign(contact_day=_to_ts(contacts["sent_at"]))[["account_id", "contact_day"]]
    treated = ep[["account_id", "decision_date"]].merge(ct, on="account_id", how="left")
    treated_ids = set(treated.loc[treated["contact_day"] == treated["decision_date"], "account_id"])
    ep["treated"] = ep["account_id"].isin(treated_ids)

    # ---- entity view (the thesis): sibling accounts of the same resolved entity, as of D
    ep = _add_entity_features(ep, accounts, st)

    ep["log_exposure"] = np.log(ep["exposure"].astype(float))
    ep["months_on_book"] = (ep["decision_date"] - ep["origination_date"]).dt.days / 30.4
    ep["log_overdue_amount"] = np.log1p(ep["overdue_amount"])
    for product in PRODUCTS:
        ep[f"product_{product}"] = (ep["product"] == product).astype(int)
    ep["labelled"] = ep["decision_date"] + pd.Timedelta(days=CURE_WINDOW_DAYS) <= as_of_ts

    columns = ID_COLUMNS + FEATURES + LABEL_COLUMNS
    out = ep[columns].copy()
    out["decision_date"] = out["decision_date"].dt.date
    return out.sort_values(["decision_date", "account_id"]).reset_index(drop=True)


def _add_entity_features(
    ep: pd.DataFrame, accounts: pd.DataFrame, states: pd.DataFrame
) -> pd.DataFrame:
    acc = accounts.assign(
        origination_date=_day(accounts["origination_date"]),
        exposure=accounts["exposure"].astype(float),
    )
    pairs = ep[["account_id", "entity_id", "decision_date"]].merge(
        acc[["account_id", "entity_id", "origination_date", "exposure"]].rename(
            columns={"account_id": "other_account_id"}
        ),
        on="entity_id",
        how="left",
    )
    pairs = pairs[pairs["origination_date"] <= pairs["decision_date"]]

    # state of every sibling account as of the decision date (latest effective_from <= D)
    sorted_pairs = pairs.sort_values("decision_date")
    sorted_states = states[["account_id", "effective_from", "state"]].sort_values("effective_from")
    with_state = pd.merge_asof(
        sorted_pairs,
        sorted_states.rename(columns={"account_id": "other_account_id"}),
        left_on="decision_date",
        right_on="effective_from",
        by="other_account_id",
        direction="backward",
    )
    with_state["other_delinquent"] = with_state["state"].isin(DELINQUENT_STATES) & (
        with_state["other_account_id"] != with_state["account_id"]
    )
    agg = with_state.groupby("account_id").agg(
        entity_n_accounts=("other_account_id", "nunique"),
        entity_total_exposure=("exposure", "sum"),
        entity_n_other_delinquent=("other_delinquent", "sum"),
    )
    ep = ep.merge(agg.reset_index(), on="account_id", how="left")
    ep["entity_n_accounts"] = ep["entity_n_accounts"].fillna(1.0)
    ep["entity_total_exposure"] = ep["entity_total_exposure"].fillna(ep["exposure"].astype(float))
    ep["entity_n_other_delinquent"] = ep["entity_n_other_delinquent"].fillna(0.0)
    ep["entity_log_total_exposure"] = np.log(ep["entity_total_exposure"])
    return ep


def load_episode_frame(session: Session, as_of: date) -> pd.DataFrame:
    """Reads the four source tables and builds the episode frame."""
    connection = session.connection()
    accounts = pd.read_sql(
        select(
            Account.account_id,
            Account.entity_id,
            Account.product,
            Account.exposure,
            Account.origination_date,
        ),
        connection,
    )
    payments = pd.read_sql(
        select(
            PaymentEvent.account_id,
            PaymentEvent.due_date,
            PaymentEvent.paid_date,
            PaymentEvent.amount_due,
            PaymentEvent.amount_paid,
        ),
        connection,
    )
    states = pd.read_sql(
        select(
            DelinquencyState.account_id,
            DelinquencyState.effective_from,
            DelinquencyState.state,
        ),
        connection,
    )
    contacts = pd.read_sql(
        select(ContactEvent.account_id, ContactEvent.sent_at).where(
            ContactEvent.account_id.is_not(None)
        ),
        connection,
    )
    return build_episode_frame(accounts, payments, states, contacts, as_of)
