"""Causal outcome model for delinquency episodes — the ground truth Phase 4 must recover.

Why this exists: CLAUDE.md §8 asks for a simulated historical contact policy "so the uplift
models have a treatment variable". A treatment variable is only useful if the outcome
actually depends on it. In the first Phase 1 build contact was simulated *after* payments
were fixed, so the true effect of contact was zero for every account and any uplift model
(and Metric 2) would have been fitting noise. Here the outcome is drawn *from* the
treatment.

Outcome: the account "cures" (pays every overdue installment) within CURE_WINDOW_DAYS of the
decision date (the day it first reaches 30 DPD). Treatment: being contacted on that day.

    logit P(cure | x, t) = BASE(x) + t * EFFECT(x)

x = (discipline d, standardised log-exposure z_e, standardised months-on-book z_m). d is
latent (never stored); the observable proxy is how late the account paid before the
episode (continuum.foundation.generate). Treatment depends on exposure only (the rules
policy, continuum.foundation.contact_policy), so given exposure, treatment is independent of
d — the unconfoundedness a T-learner needs — and the historical policy has overlap
(0.1 <= P(contact) <= 0.9).

Heterogeneity is deliberate, including sign changes ("sleeping dogs"): contact helps
mid-exposure, less-disciplined borrowers most, and slightly hurts very disciplined or
extreme-exposure ones. Without it, ranking by uplift could not beat ranking by exposure.

The true per-account P(cure|no contact) and P(cure|contact) are written to an eval-only
file (data/true_uplift.csv, see continuum.foundation.build) — invisible to runtime code,
same rule as ground_truth_link (CLAUDE.md I4).

All coefficients are properties of the synthetic dataset, not runtime business thresholds
(CLAUDE.md §10), hence constants here rather than Settings.
"""

from __future__ import annotations

import math

CURE_WINDOW_DAYS = 60

# Baseline (no-contact) cure: ~45% on average; disciplined borrowers self-cure more, large
# loans and short tenures a bit less.
BASE_INTERCEPT = -0.2
BASE_DISCIPLINE = 0.8
BASE_EXPOSURE = -0.3
BASE_MONTHS_ON_BOOK = 0.25

# Treatment effect on the log-odds scale: positive on average, larger for low discipline,
# concave in exposure (peaks near +0.5 sd), negative in the tails.
EFFECT_INTERCEPT = 0.9
EFFECT_DISCIPLINE = -0.5
EFFECT_EXPOSURE = 0.4
EFFECT_EXPOSURE_SQ = -0.3

# Share of accounts not cured inside the window that pay late instead (days 61-89 after the
# decision date, still before the 120-DPD charge-off).
LATE_CURE_PROBABILITY = 0.15


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def cure_probabilities(
    discipline: float, z_exposure: float, z_months_on_book: float
) -> tuple[float, float]:
    """(P(cure | no contact), P(cure | contact)) for one episode."""
    base = (
        BASE_INTERCEPT
        + BASE_DISCIPLINE * discipline
        + BASE_EXPOSURE * z_exposure
        + BASE_MONTHS_ON_BOOK * z_months_on_book
    )
    effect = (
        EFFECT_INTERCEPT
        + EFFECT_DISCIPLINE * discipline
        + EFFECT_EXPOSURE * z_exposure
        + EFFECT_EXPOSURE_SQ * z_exposure**2
    )
    return sigmoid(base), sigmoid(base + effect)
