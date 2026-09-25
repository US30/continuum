"""The generator's causal episodes must agree with what the delinquency state machine
derives from the payments, and the treatment must genuinely change the outcome — otherwise
Phase 4's uplift labels and Metric 2 would be built on noise.
"""

from __future__ import annotations

import statistics
from datetime import timedelta

from continuum.db.models.account import DelinquencyBucket
from continuum.foundation import causal
from continuum.foundation.contact_policy import contact_probability
from continuum.foundation.delinquency import derive_delinquency_states
from continuum.foundation.generate import AS_OF_DATE, generate_population

PERSONS = generate_population(2_000, seed=42)
EPISODES = [(a, a.episode) for p in PERSONS for a in p.accounts if a.episode is not None]


def test_generation_is_deterministic() -> None:
    assert generate_population(50, seed=7) == generate_population(50, seed=7)


def test_decision_date_is_when_the_state_machine_reaches_30_dpd() -> None:
    for account, episode in EPISODES[:500]:
        states = derive_delinquency_states(account.payments, account.origination_date, AS_OF_DATE)
        first_30 = next(s for s in states if s.state == DelinquencyBucket.DPD_30)
        assert first_30.effective_from == episode.decision_date


def test_cure_inside_the_window_derives_a_cured_state() -> None:
    checked = 0
    for account, episode in EPISODES:
        if not episode.cured_60 or episode.cure_date is None or episode.cure_date > AS_OF_DATE:
            continue
        states = derive_delinquency_states(account.payments, account.origination_date, AS_OF_DATE)
        assert any(s.state == DelinquencyBucket.CURED for s in states)
        assert episode.cure_date - episode.decision_date <= timedelta(days=causal.CURE_WINDOW_DAYS)
        checked += 1
    assert checked > 100


def test_uncured_episodes_are_still_delinquent_or_charged_off() -> None:
    for account, episode in EPISODES:
        if episode.cure_date is not None and episode.cure_date <= AS_OF_DATE:
            continue
        state = derive_delinquency_states(account.payments, account.origination_date, AS_OF_DATE)[
            -1
        ].state
        assert state in {
            DelinquencyBucket.DPD_30,
            DelinquencyBucket.DPD_60,
            DelinquencyBucket.DPD_90,
            DelinquencyBucket.CHARGED_OFF,
        }


def test_treatment_has_a_real_positive_average_effect() -> None:
    ate = statistics.mean(e.p_cure_treated - e.p_cure_control for _, e in EPISODES)
    assert 0.05 < ate < 0.4


def test_treatment_effect_is_heterogeneous_including_negative() -> None:
    effects = [e.p_cure_treated - e.p_cure_control for _, e in EPISODES]
    assert min(effects) < 0 < max(effects)
    assert statistics.pstdev(effects) > 0.05


def test_realised_outcomes_follow_the_causal_probabilities() -> None:
    treated = [e for _, e in EPISODES if e.treated]
    untreated = [e for _, e in EPISODES if not e.treated]
    assert (
        abs(
            statistics.mean(e.cured_60 for e in treated)
            - statistics.mean(e.p_cure_treated for e in treated)
        )
        < 0.04
    )
    assert (
        abs(
            statistics.mean(e.cured_60 for e in untreated)
            - statistics.mean(e.p_cure_control for e in untreated)
        )
        < 0.04
    )


def test_treatment_has_overlap_and_depends_on_exposure_only() -> None:
    probabilities = [contact_probability(float(a.exposure), None) for a, _ in EPISODES]  # type: ignore[arg-type]
    assert min(probabilities) >= 0.1 and max(probabilities) <= 0.9
    high = [e.treated for a, e in EPISODES if a.exposure > 8_000]
    low = [e.treated for a, e in EPISODES if a.exposure < 2_000]
    assert statistics.mean(high) > statistics.mean(low)
