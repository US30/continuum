"""Metric 3 — share of collections cases where the entity view beat the account view.

CLAUDE.md §9: "Share of cases where the entity view beat the account view — this is the number
that justifies building the two halves as one system. It is the headline." CLAUDE.md Phase 7
done-when: the share of collections cases where the entity view surfaced a linked account, a
common controlling party or a fraud signature "the account view would have missed".
Run via `make network-metrics` (`python -m eval.network_value`).

A COLLECTIONS CASE is a resolved entity with at least one currently delinquent account.

THE ACCOUNT VIEW (baseline, written down as CLAUDE.md requires): an account-centric system
links two accounts only when the identity records they were opened under carry the *exact same
national_id string*, and it sees nothing across customers (no shared-address/phone network).
Formatting drift (`123-45-6789` vs `123456789`) or a blank id defeats it.

WHAT THE ENTITY VIEW SURFACES, per case (a case counts if any of the three is true):
    L  linked account      the entity's delinquent accounts fall into more than one
                           account-view group, i.e. resolution linked accounts an
                           account-centric system would have handled as separate customers;
    C  controlling party   the entity has a strong network link (shared address or phone, from
                           entity_edge) to another entity that is itself delinquent — a
                           cross-customer relationship no account view can see;
    F  fraud signature     the deterministic network fraud score is outside the "clear" band
                           (continuum.graph.nodes.prioritise.fraud_signals).

RAW share counts a case if L or C or F holds. TRUTH-VALIDATED share (the headline) counts a
component only if it is *real* according to the eval-only ground truth:
    L valid  two accounts in different account-view groups truly belong to the same person;
    C valid  the linked entities truly share a planted household or ring;
    F valid  the case's people truly belong to a planted fraud ring.
Validation stops a resolver that merges strangers, or a fraud score that fires on families,
from inflating the number. The truth files (account_truth / record_truth / network_truth) are
eval-only (CLAUDE.md I4); this module is one of the only readers.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from continuum.config.settings import get_settings
from continuum.db.models.account import Account
from continuum.db.models.entity import EntityEdge, EntityMember
from continuum.db.models.party import PartyRecord
from continuum.db.session import session_scope
from continuum.graph.nodes.case_assembly import DELINQUENT
from continuum.graph.nodes.prioritise import STRONG_EDGE_TYPES, fraud_band, fraud_signals
from continuum.graph.state import LinkedEntity

DATA = Path("data")
ACCOUNT_TRUTH = DATA / "account_truth.csv"
RECORD_TRUTH = DATA / "record_truth.csv"
NETWORK_TRUTH = DATA / "network_truth.csv"


@dataclass(frozen=True)
class Truth:
    account_record: dict[str, str]  # account_id -> record_id
    record_person: dict[str, str]  # record_id -> person_id
    person_group: dict[str, tuple[str, str]]  # person_id -> (group_id, group_type)


def load_truth(
    account_path: Path = ACCOUNT_TRUTH,
    record_path: Path = RECORD_TRUTH,
    network_path: Path = NETWORK_TRUTH,
) -> Truth:
    def rows(path: Path) -> list[dict[str, str]]:
        with path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    return Truth(
        account_record={r["account_id"]: r["record_id"] for r in rows(account_path)},
        record_person={r["record_id"]: r["person_id"] for r in rows(record_path)},
        person_group={r["person_id"]: (r["group_id"], r["group_type"]) for r in rows(network_path)},
    )


@dataclass(frozen=True)
class CaseInput:
    """Everything Metric 3 needs about one collections case."""

    entity_id: str
    account_ids: tuple[str, ...]
    #: account_id -> exact national_id of the record it was opened under (None if blank)
    account_national_id: dict[str, str | None]
    #: account_id -> record it was opened under (for truth validation only)
    account_record: dict[str, str]
    links: tuple[LinkedEntity, ...]
    people: frozenset[str]  # true persons behind this entity's records (truth)
    linked_people: dict[str, frozenset[str]]  # linked entity id -> its true persons (truth)


def account_view_groups(case: CaseInput) -> dict[str, str]:
    """account_id -> group key under the exact-national_id baseline. A blank id is its own
    group (the account-centric system has nothing to link it on)."""
    return {
        a: (f"id:{nid}" if (nid := case.account_national_id[a]) else f"solo:{a}")
        for a in case.account_ids
    }


@dataclass(frozen=True)
class CaseResult:
    entity_id: str
    linked_account: bool
    controlling_party: bool
    fraud_signature: bool
    linked_account_valid: bool
    controlling_party_valid: bool
    fraud_signature_valid: bool
    is_ring_case: bool

    @property
    def raw(self) -> bool:
        return self.linked_account or self.controlling_party or self.fraud_signature

    @property
    def validated(self) -> bool:
        return (
            self.linked_account_valid or self.controlling_party_valid or self.fraud_signature_valid
        )


def evaluate_case(case: CaseInput, truth: Truth) -> CaseResult:
    settings = get_settings()
    groups = account_view_groups(case)

    # L — accounts the account view keeps apart but the entity holds together.
    distinct_groups = set(groups.values())
    linked_account = len(case.account_ids) > 1 and len(distinct_groups) > 1
    linked_valid = False
    if linked_account:
        by_group: dict[str, set[str]] = defaultdict(set)
        for account_id, group in groups.items():
            by_group[group].add(truth.record_person[case.account_record[account_id]])
        # Some pair of accounts in different account-view groups is genuinely one person.
        linked_valid = any(
            (a & b) for g1, a in by_group.items() for g2, b in by_group.items() if g1 < g2
        )

    # C — a strong link to another delinquent entity.
    strong_delinquent = [
        le for le in case.links if le.edge_type in STRONG_EDGE_TYPES and le.delinquent
    ]
    controlling_party = bool(strong_delinquent)
    my_groups = {truth.person_group[p][0] for p in case.people if p in truth.person_group}
    controlling_valid = any(
        my_groups
        & {
            truth.person_group[p][0]
            for p in case.linked_people.get(str(le.entity_id), frozenset())
            if p in truth.person_group
        }
        for le in strong_delinquent
    )

    # F — the deterministic fraud score outside the clear band.
    score, *_ = fraud_signals(list(case.links))
    fraud = fraud_band(score, settings) != "clear"
    ring_case = any(truth.person_group.get(p, ("", ""))[1] == "ring" for p in case.people)

    return CaseResult(
        entity_id=case.entity_id,
        linked_account=linked_account,
        controlling_party=controlling_party,
        fraud_signature=fraud,
        linked_account_valid=linked_valid,
        controlling_party_valid=controlling_valid,
        fraud_signature_valid=fraud and ring_case,
        is_ring_case=ring_case,
    )


def load_cases(session: Session, truth: Truth) -> list[CaseInput]:
    delinquent = session.execute(
        select(Account.account_id, Account.entity_id).where(Account.current_state.in_(DELINQUENT))
    ).all()
    entity_accounts: dict[str, list[str]] = defaultdict(list)
    for account_id, entity_id in delinquent:
        entity_accounts[str(entity_id)].append(str(account_id))

    record_national_id = {
        str(record_id): national_id
        for record_id, national_id in session.execute(
            select(PartyRecord.record_id, PartyRecord.national_id)
        )
    }
    members: dict[str, set[str]] = defaultdict(set)
    for entity_id, record_id in session.execute(
        select(EntityMember.entity_id, EntityMember.record_id)
    ):
        members[str(entity_id)].add(str(record_id))

    def persons_of(entity_id: str) -> frozenset[str]:
        return frozenset(
            truth.record_person[r] for r in members.get(entity_id, ()) if r in truth.record_person
        )

    delinquent_entities = set(entity_accounts)
    links: dict[str, list[LinkedEntity]] = defaultdict(list)
    for edge in session.scalars(select(EntityEdge)):
        a, b = str(edge.entity_id_a), str(edge.entity_id_b)
        for me, other in ((a, b), (b, a)):
            links[me].append(
                LinkedEntity(
                    entity_id=other,  # type: ignore[arg-type]
                    edge_type=edge.edge_type.value,
                    strength=float(edge.strength),
                    delinquent=other in delinquent_entities,
                )
            )

    cases: list[CaseInput] = []
    for entity_id in sorted(entity_accounts):
        account_ids = tuple(sorted(entity_accounts[entity_id]))
        account_record = {a: truth.account_record[a] for a in account_ids}
        neighbours = {str(le.entity_id) for le in links[entity_id]}
        cases.append(
            CaseInput(
                entity_id=entity_id,
                account_ids=account_ids,
                account_national_id={
                    a: record_national_id.get(account_record[a]) for a in account_ids
                },
                account_record=account_record,
                links=tuple(links[entity_id]),
                people=persons_of(entity_id),
                linked_people={n: persons_of(n) for n in neighbours},
            )
        )
    return cases


@dataclass(frozen=True)
class Metric3Report:
    n_cases: int
    n_delinquent_accounts: int
    n_multi_account_cases: int
    # headline
    validated_share: float
    raw_share: float
    # components, raw and validated
    linked_account_share: float
    linked_account_valid_share: float
    controlling_party_share: float
    controlling_party_valid_share: float
    fraud_signature_share: float
    fraud_signature_valid_share: float
    # fraud-signature quality against the planted rings
    ring_cases: int
    ring_cases_flagged: int
    non_ring_cases_flagged: int
    non_ring_cases: int

    @property
    def outreach_saved(self) -> int:
        """Accounts folded into a case that already existed: parallel dunning avoided."""
        return self.n_delinquent_accounts - self.n_cases


def compute_metric3(cases: list[CaseInput], truth: Truth) -> Metric3Report:
    results = [evaluate_case(c, truth) for c in cases]
    n = max(len(results), 1)

    def share(pred: object) -> float:
        return sum(1 for r in results if pred(r)) / n  # type: ignore[operator]

    ring = [r for r in results if r.is_ring_case]
    other = [r for r in results if not r.is_ring_case]
    return Metric3Report(
        n_cases=len(results),
        n_delinquent_accounts=sum(len(c.account_ids) for c in cases),
        n_multi_account_cases=sum(1 for c in cases if len(c.account_ids) > 1),
        validated_share=share(lambda r: r.validated),
        raw_share=share(lambda r: r.raw),
        linked_account_share=share(lambda r: r.linked_account),
        linked_account_valid_share=share(lambda r: r.linked_account_valid),
        controlling_party_share=share(lambda r: r.controlling_party),
        controlling_party_valid_share=share(lambda r: r.controlling_party_valid),
        fraud_signature_share=share(lambda r: r.fraud_signature),
        fraud_signature_valid_share=share(lambda r: r.fraud_signature_valid),
        ring_cases=len(ring),
        ring_cases_flagged=sum(1 for r in ring if r.fraud_signature),
        non_ring_cases=len(other),
        non_ring_cases_flagged=sum(1 for r in other if r.fraud_signature),
    )


def report(as_json: bool = False) -> Metric3Report:
    truth = load_truth()
    with session_scope() as session:
        cases = load_cases(session, truth)
    result = compute_metric3(cases, truth)
    if as_json:
        print(json.dumps(asdict(result)))
        return result

    def pct(x: float) -> str:
        return f"{100 * x:5.1f}%"

    print("")
    print("=== CONTINUUM Metric 3: share of collections cases where the entity view beat ===")
    print("=== the account view (account view = exact national_id links, no network)      ===")
    print(
        f"collections cases: {result.n_cases}   delinquent accounts: {result.n_delinquent_accounts}"
        f"   multi-account cases: {result.n_multi_account_cases}"
    )
    print(f"parallel outreach avoided by collapsing accounts into cases: {result.outreach_saved}")
    print("")
    print(f"{'':<34}{'raw':>8}{'truth-validated':>18}")
    print(
        f"{'L linked account':<34}{pct(result.linked_account_share):>8}"
        f"{pct(result.linked_account_valid_share):>18}"
    )
    print(
        f"{'C common controlling party':<34}{pct(result.controlling_party_share):>8}"
        f"{pct(result.controlling_party_valid_share):>18}"
    )
    print(
        f"{'F fraud signature':<34}{pct(result.fraud_signature_share):>8}"
        f"{pct(result.fraud_signature_valid_share):>18}"
    )
    headline = f"{pct(result.raw_share):>8}{pct(result.validated_share):>18}"
    print(f"{'METRIC 3 (any of L, C, F)':<34}{headline}")
    print("")
    print(
        f"fraud signature vs planted rings: flagged {result.ring_cases_flagged}/{result.ring_cases}"
        f" ring cases; {result.non_ring_cases_flagged}/{result.non_ring_cases} non-ring cases"
    )
    print("")
    return result


if __name__ == "__main__":
    import sys

    report(as_json="--json" in sys.argv)
