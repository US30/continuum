"""scoring — self-cure P, response P, uplift per account. No LLM (CLAUDE.md I1, §7)."""

from __future__ import annotations

from typing import Any

from continuum.graph.deps import GraphDeps
from continuum.graph.state import CaseState
from continuum.telemetry.spans import node_span


def score_case(state: CaseState, deps: GraphDeps) -> dict[str, Any]:
    with node_span("scoring", state.entity_id):
        assert state.case is not None
        scores = deps.scorer.score([a.account_id for a in state.case.accounts])
        accounts = []
        for account in state.case.accounts:
            found = scores.get(account.account_id)
            if found is None:
                accounts.append(account)  # no episode -> unscored; contributes 0 recovery
                continue
            accounts.append(
                account.model_copy(
                    update={
                        "p_cure_no_contact": found.p_cure_no_contact,
                        "p_cure_contact": found.p_cure_contact,
                        "uplift": found.uplift,
                    }
                )
            )
        case = state.case.model_copy(update={"accounts": accounts})
        n_scored = sum(a.uplift is not None for a in accounts)
        return {"case": case, "events": [f"scoring: {n_scored}/{len(accounts)} accounts scored"]}
