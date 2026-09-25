"""The generator's target profile must match the state the delinquency state machine derives
from its payments — otherwise Phase 4's cure/response labels silently disagree with the
data the generator claims to have made.
"""

from __future__ import annotations

from continuum.db.models.account import DelinquencyBucket
from continuum.foundation.delinquency import derive_delinquency_states
from continuum.foundation.generate import AS_OF_DATE, DelinquencyProfile, generate_population

EXPECTED_STATE = {
    DelinquencyProfile.CLEAN: DelinquencyBucket.CURRENT,
    DelinquencyProfile.DELINQUENT_30: DelinquencyBucket.DPD_30,
    DelinquencyProfile.DELINQUENT_60: DelinquencyBucket.DPD_60,
    DelinquencyProfile.DELINQUENT_90: DelinquencyBucket.DPD_90,
    DelinquencyProfile.CURED: DelinquencyBucket.CURED,
    DelinquencyProfile.CHARGED_OFF: DelinquencyBucket.CHARGED_OFF,
}


def test_every_account_derives_the_state_its_profile_targets() -> None:
    persons = generate_population(1_000, seed=42)
    mismatches = []
    for person in persons:
        for account in person.accounts:
            state = derive_delinquency_states(
                account.payments, account.origination_date, AS_OF_DATE
            )[-1].state
            if state != EXPECTED_STATE[account.profile]:
                mismatches.append((account.profile, state))
    assert not mismatches, mismatches[:5]


def test_generation_is_deterministic() -> None:
    assert generate_population(50, seed=7) == generate_population(50, seed=7)
