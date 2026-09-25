"""prioritise — expected recovery net of contact cost, plus the network fraud decision.

CLAUDE.md §7: "rank by expected recovery net of contact cost; network fraud signature ->
divert out of collections entirely". Numbers and the fraud *score* are deterministic (I1's
spirit: the LLM never invents a figure). The model is used exactly once, and only in the
ambiguous fraud band, like the screening adjudicator: below `fraud_score_review` the case is
ordinary collections, at/above `fraud_score_divert` it is diverted, and in between the model
decides. If the model fails in that band the case is NOT sent: it is flagged for human review.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from continuum.config.settings import Settings
from continuum.graph.deps import GraphDeps
from continuum.graph.schemas import FraudAdjudication
from continuum.graph.state import CaseState, CaseView, FraudBand, LinkedEntity, Priority
from continuum.telemetry.spans import NodeUsage, node_span

PROMPT_DIR = Path(__file__).parent.parent / "prompts"
PROMPT_VERSION = "prioritise_v1"
_SYSTEM = (PROMPT_DIR / f"{PROMPT_VERSION}_system.txt").read_text(encoding="utf-8")
_USER = (PROMPT_DIR / f"{PROMPT_VERSION}_user.txt").read_text(encoding="utf-8")

STRONG_EDGE_TYPES = frozenset({"shared_address", "shared_phone"})
# Number of strong links at which each half of the score saturates (see Settings.fraud_*).
SATURATION_LINKS = 3


def fraud_signals(linked: list[LinkedEntity]) -> tuple[float, int, int, int]:
    """(score, strong links, strong links also delinquent, weak links). Pure. Links are
    counted per *distinct linked entity*: two edge types to one neighbour is one neighbour."""
    strong = {le.entity_id for le in linked if le.edge_type in STRONG_EDGE_TYPES}
    strong_delinquent = {
        le.entity_id for le in linked if le.edge_type in STRONG_EDGE_TYPES and le.delinquent
    }
    weak = {le.entity_id for le in linked} - strong
    score = 0.5 * min(len(strong) / SATURATION_LINKS, 1.0) + 0.5 * min(
        len(strong_delinquent) / SATURATION_LINKS, 1.0
    )
    return score, len(strong), len(strong_delinquent), len(weak)


def fraud_band(score: float, settings: Settings) -> FraudBand:
    if score >= settings.fraud_score_divert:
        return "signature"
    if score >= settings.fraud_score_review:
        return "ambiguous"
    return "clear"


def expected_incremental_recovery(case: CaseView) -> float:
    """One outreach covers the whole case, so the expected gain is the sum over its accounts.
    Negative when contact is expected to hurt (the 'sleeping dog' accounts)."""
    return sum(a.expected_incremental_recovery for a in case.accounts)


def _adjudicate_fraud(
    case: CaseView, score: float, counts: tuple[int, int, int], deps: GraphDeps, usage: NodeUsage
) -> FraudAdjudication:
    n_strong, n_strong_delinquent, n_weak = counts
    user = _USER.format(
        n_accounts=len(case.accounts),
        total_overdue=f"{case.total_overdue:,.2f}",
        max_dpd=case.max_days_past_due,
        n_strong=n_strong,
        n_strong_delinquent=n_strong_delinquent,
        n_weak=n_weak,
        fraud_score=f"{score:.2f}",
    )
    result = deps.llm.complete("prioritise", _SYSTEM, user, FraudAdjudication)
    u = result.usage
    usage.record(u.model, u.input_tokens, u.output_tokens, u.cost_usd, u.calls)
    return result.parsed


def prioritise_case(state: CaseState, deps: GraphDeps) -> dict[str, Any]:
    with node_span("prioritise", state.entity_id) as usage:
        assert state.case is not None
        case, settings = state.case, deps.settings

        gain = expected_incremental_recovery(case)
        cost = settings.contact_cost_per_case
        score, n_strong, n_strong_delinquent, n_weak = fraud_signals(case.linked_entities)
        band = fraud_band(score, settings)

        divert = band == "signature"
        needs_review = False
        llm_used = False
        rationale = (
            f"fraud score {score:.2f} ({n_strong} strong links, {n_strong_delinquent} delinquent)"
        )
        if band == "signature":
            rationale += ": above the divert threshold"
        elif band == "ambiguous":
            try:
                verdict = _adjudicate_fraud(
                    case, score, (n_strong, n_strong_delinquent, n_weak), deps, usage
                )
                llm_used = True
                divert = verdict.divert
                rationale += f": model says divert={verdict.divert} ({verdict.reasoning})"
            except Exception as exc:  # a model outage must not silently decide a fraud case
                needs_review = True
                rationale += f": model unavailable ({type(exc).__name__}); flagged for human review"

        priority = Priority(
            expected_incremental_recovery=gain,
            contact_cost=cost,
            expected_net_recovery=gain - cost,
            fraud_score=score,
            fraud_band=band,
            divert=divert,
            needs_review=needs_review,
            rationale=rationale,
            llm_used=llm_used,
        )
        return {
            "priority": priority,
            "events": [
                f"prioritise: net={priority.expected_net_recovery:+.2f} "
                f"fraud={band} divert={divert}"
            ],
        }
