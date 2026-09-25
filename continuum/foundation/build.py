"""Phase 1 entrypoint: builds the full local dataset from scratch.

Run via `make data-build` (`python -m continuum.foundation.build`). Idempotent (CLAUDE.md
I6): every writer upserts on a natural key — party_record on (source_system,
source_record_id), entity on a record_id-derived id, etc. — so re-running with the same
DATA_SEED changes no row counts.

Architecture note — flagged, not silently resolved (CLAUDE.md §11: "ask before assuming
on anything touching the data model"), confirmed with the user before Phase 2 was written:
CLAUDE.md §6 gives `account` a NOT NULL FK straight to `entity`, but entity resolution
(Phase 2) does not exist until after this loader runs. This loader therefore gives every
party_record its own *singleton* Entity at load time (cluster_size=1, confidence=1.0, a
fixed sentinel resolution_run_id) and one entity_member row per record.

Deliberately one singleton per *record*, not one per synthetic person: this loader has no
`person_id` column to read at all in a real system — that field only exists in-memory here
because we generated the ground truth ourselves. Keying entity assignment off it instead of
off the record would silently pre-merge exactly the collisions continuum.resolution (Phase
2) is supposed to discover from observable attributes, making Phase 2 vacuous against this
dataset. So: each account is created under its *canonical* (loan_origination) record's
singleton entity — the identity the loan was actually originated under — and every
duplicate/hard-negative record from continuum.foundation.collisions sits in its own
unconnected singleton until Phase 2 merges it in. continuum.resolution.merge repoints every
FK'd row (account, contact_event, risk_file_event, case) from an absorbed singleton to the
surviving entity_id and deletes the absorbed Entity row; the surviving id is the
deterministic cluster id from continuum.resolution.cluster, so which record happened to be
"canonical" doesn't matter for correctness.
"""

from __future__ import annotations

import random
import uuid
from collections import Counter
from decimal import Decimal
from pathlib import Path

import typer
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from continuum.config.settings import get_settings
from continuum.db.models.account import Account, DelinquencyState, PaymentEvent
from continuum.db.models.collections import ContactEvent, ContactSource
from continuum.db.models.entity import Entity, EntityMember
from continuum.db.models.eval import GroundTruthLink
from continuum.db.models.party import PartyRecord
from continuum.db.models.screening import SanctionsAlias, SanctionsEntity
from continuum.db.session import session_scope
from continuum.foundation import (
    collisions,
    contact_policy,
    delinquency,
    generate,
    sanctions,
    synthetic_sanctions,
    true_uplift,
)
from continuum.telemetry.logging import configure_logging, get_logger

app = typer.Typer(add_completion=False)
logger = get_logger(__name__)

DEFAULT_N_PERSONS = 5_000
SANCTIONS_CSV = Path("data") / "targets.simple.csv"
SANCTIONS_PLANTED_CSV = Path("data") / "sanctions_planted.csv"  # eval-only truth (I4)
TRUE_UPLIFT_CSV = Path("data") / "true_uplift.csv"  # eval-only truth (I4)

# Fixed sentinel for the Phase-1 singleton entities described above — never used once
# Phase 2's real resolution_run_id values exist, kept constant so re-runs are idempotent.
SINGLETON_RESOLUTION_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _singleton_entity_id(record_id: uuid.UUID) -> uuid.UUID:
    """Deterministic entity id for one party_record's Phase-1 singleton entity (CLAUDE.md
    I6). Keyed on record_id, not on the in-memory-only person_id — see the module
    docstring for why that distinction is load-bearing for Phase 2.
    """
    return uuid.uuid5(record_id, "phase1-singleton")


def _load_party_records_and_links(
    session: Session, drafts: list[collisions.PartyRecordDraft], links: list[collisions.PlantedLink]
) -> None:
    for draft in drafts:
        stmt = pg_insert(PartyRecord).values(
            record_id=draft.record_id,
            source_system=draft.source_system,
            source_record_id=draft.source_record_id,
            full_name=draft.identity.full_name,
            date_of_birth=draft.identity.date_of_birth,
            national_id=draft.identity.national_id,
            address_line=draft.identity.address_line,
            postal_code=draft.identity.postal_code,
            phone=draft.identity.phone,
            email=draft.identity.email,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["source_system", "source_record_id"],
            set_={
                "full_name": stmt.excluded.full_name,
                "date_of_birth": stmt.excluded.date_of_birth,
                "national_id": stmt.excluded.national_id,
                "address_line": stmt.excluded.address_line,
                "postal_code": stmt.excluded.postal_code,
                "phone": stmt.excluded.phone,
                "email": stmt.excluded.email,
            },
        )
        session.execute(stmt)

    for link in links:
        stmt = pg_insert(GroundTruthLink).values(
            link_id=uuid.uuid5(link.record_id_a, str(link.record_id_b)),
            record_id_a=link.record_id_a,
            record_id_b=link.record_id_b,
            collision_type=link.collision_type,
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["link_id"])
        session.execute(stmt)


def _load_singletons(
    session: Session, drafts: list[collisions.PartyRecordDraft]
) -> dict[uuid.UUID, uuid.UUID]:
    """One singleton Entity + one EntityMember row per party_record. Returns
    {person_id: canonical_entity_id} — the loan_origination record's entity id for each
    person — for the account/contact loaders below, which is the only person-level fact
    this module is allowed to know at write time (see the module docstring).
    """
    canonical_entity_by_person: dict[uuid.UUID, uuid.UUID] = {}

    for draft in drafts:
        entity_id = _singleton_entity_id(draft.record_id)

        entity_stmt = pg_insert(Entity).values(
            entity_id=entity_id,
            cluster_size=1,
            confidence=Decimal("1.0"),
            resolution_run_id=SINGLETON_RESOLUTION_RUN_ID,
        )
        entity_stmt = entity_stmt.on_conflict_do_nothing(index_elements=["entity_id"])
        session.execute(entity_stmt)

        member_stmt = pg_insert(EntityMember).values(
            entity_id=entity_id,
            record_id=draft.record_id,
            match_probability=Decimal("1.0"),
        )
        member_stmt = member_stmt.on_conflict_do_nothing(index_elements=["entity_id", "record_id"])
        session.execute(member_stmt)

        if draft.source_system == "loan_origination":
            canonical_entity_by_person[draft.person_id] = entity_id

    return canonical_entity_by_person


def _load_accounts(
    session: Session,
    persons: list[generate.SyntheticPerson],
    canonical_entity_by_person: dict[uuid.UUID, uuid.UUID],
) -> None:
    for person in persons:
        entity_id = canonical_entity_by_person[person.person_id]

        for account in person.accounts:
            states = delinquency.derive_delinquency_states(
                account.payments, account.origination_date, generate.AS_OF_DATE
            )

            account_stmt = pg_insert(Account).values(
                account_id=account.account_id,
                entity_id=entity_id,
                product=account.product,
                exposure=account.exposure,
                origination_date=account.origination_date,
                current_state=states[-1].state,
            )
            account_stmt = account_stmt.on_conflict_do_update(
                index_elements=["account_id"],
                set_={"current_state": account_stmt.excluded.current_state},
            )
            session.execute(account_stmt)

            for payment in account.payments:
                payment_stmt = pg_insert(PaymentEvent).values(
                    event_id=uuid.uuid5(account.account_id, str(payment.due_date)),
                    account_id=account.account_id,
                    due_date=payment.due_date,
                    paid_date=payment.paid_date,
                    amount_due=payment.amount_due,
                    amount_paid=payment.amount_paid,
                )
                payment_stmt = payment_stmt.on_conflict_do_update(
                    index_elements=["event_id"],
                    set_={
                        "paid_date": payment_stmt.excluded.paid_date,
                        "amount_paid": payment_stmt.excluded.amount_paid,
                    },
                )
                session.execute(payment_stmt)

            for interval in states:
                state_stmt = pg_insert(DelinquencyState).values(
                    account_id=account.account_id,
                    effective_from=interval.effective_from,
                    effective_to=interval.effective_to,
                    state=interval.state,
                    days_past_due=interval.days_past_due,
                )
                state_stmt = state_stmt.on_conflict_do_update(
                    index_elements=["account_id", "effective_from"],
                    set_={
                        "effective_to": state_stmt.excluded.effective_to,
                        "state": state_stmt.excluded.state,
                        "days_past_due": state_stmt.excluded.days_past_due,
                    },
                )
                session.execute(state_stmt)


def _load_contact_history(
    session: Session,
    persons: list[generate.SyntheticPerson],
    canonical_entity_by_person: dict[uuid.UUID, uuid.UUID],
    seed: int,
) -> Counter[str]:
    settings = get_settings()
    rng = random.Random(seed)
    channel_counts: Counter[str] = Counter()

    for person in persons:
        entity_id = canonical_entity_by_person[person.person_id]
        for account in person.accounts:
            episode = account.episode
            if episode is None or not episode.treated:
                continue
            contact = contact_policy.simulate_decision_contact(
                entity_id,
                account.account_id,
                episode.decision_date,
                episode.cured_60,
                settings,
                rng,
            )
            channel_counts[contact.channel.value] += 1
            contact_stmt = pg_insert(ContactEvent).values(
                contact_id=uuid.uuid5(account.account_id, contact.sent_at.isoformat()),
                case_id=None,
                entity_id=entity_id,
                account_id=account.account_id,
                source=ContactSource.SIMULATED_HISTORY,
                channel=contact.channel,
                sent_at=contact.sent_at,
                body_hash="synthetic",
                guard_verdict=None,
                responded=contact.responded,
                responded_at=contact.responded_at,
            )
            contact_stmt = contact_stmt.on_conflict_do_update(
                index_elements=["contact_id"],
                set_={
                    "responded": contact_stmt.excluded.responded,
                    "responded_at": contact_stmt.excluded.responded_at,
                },
            )
            session.execute(contact_stmt)
    return channel_counts


def _load_sanctions_snapshot(session: Session, csv_path: Path) -> tuple[uuid.UUID, int] | None:
    if not csv_path.exists():
        return None

    listings = sanctions.parse_targets_simple_csv(csv_path)
    snapshot_id = sanctions.new_snapshot_id()

    # Atomic swap: delete the old snapshot and insert the new one inside the same
    # transaction as the rest of the build (session_scope commits once, at the end).
    session.execute(delete(SanctionsAlias))
    session.execute(delete(SanctionsEntity))

    session.add_all(
        SanctionsEntity(
            listing_id=listing.listing_id,
            snapshot_id=snapshot_id,
            primary_name=listing.primary_name,
            schema_type=listing.schema_type,
            birth_date=listing.birth_date,
            countries=listing.countries,
            topics=listing.topics,
            is_current=True,
        )
        for listing in listings
    )
    # No ORM relationship() links alias to entity, so the unit of work does not order the
    # inserts by foreign key — flush entities first or aliases hit an FK violation.
    session.flush()

    for listing in listings:
        # dict.fromkeys: an alias identical to the primary name must not repeat (same alias_id).
        for name in dict.fromkeys([listing.primary_name, *listing.aliases]):
            session.add(
                SanctionsAlias(
                    alias_id=uuid.uuid5(uuid.NAMESPACE_URL, f"{listing.listing_id}:{name}"),
                    listing_id=listing.listing_id,
                    name=name,
                )
            )
    return snapshot_id, len(listings)


@app.command()
def build(
    n_persons: int = typer.Option(
        DEFAULT_N_PERSONS, help="Number of synthetic persons to generate."
    ),
    seed: int | None = typer.Option(None, help="Override Settings.data_seed for this run."),
) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    effective_seed = seed if seed is not None else settings.data_seed

    logger.info("data_build.start", n_persons=n_persons, seed=effective_seed)

    persons = generate.generate_population(n_persons, effective_seed)
    drafts, links = collisions.build_party_records(persons, effective_seed)

    if not SANCTIONS_CSV.exists():
        # I8: no real personal data — fall back to a generated listing. A real export
        # dropped at SANCTIONS_CSV is used as-is instead.
        n_synth = synthetic_sanctions.write_synthetic_sanctions(
            drafts, effective_seed, SANCTIONS_CSV, SANCTIONS_PLANTED_CSV
        )
        logger.info("data_build.synthetic_sanctions", n_listings=n_synth, path=str(SANCTIONS_CSV))

    true_uplift.write_true_uplift(persons, TRUE_UPLIFT_CSV)

    with session_scope() as session:
        _load_party_records_and_links(session, drafts, links)
        canonical_entity_by_person = _load_singletons(session, drafts)
        _load_accounts(session, persons, canonical_entity_by_person)
        channel_counts = _load_contact_history(
            session, persons, canonical_entity_by_person, effective_seed
        )
        sanctions_result = _load_sanctions_snapshot(session, SANCTIONS_CSV)

    _print_summary(persons, drafts, links, channel_counts, sanctions_result)


def _rate(episodes: list[generate.Episode]) -> float:
    return sum(e.cured_60 for e in episodes) / max(len(episodes), 1)


def _print_summary(
    persons: list[generate.SyntheticPerson],
    drafts: list[collisions.PartyRecordDraft],
    links: list[collisions.PlantedLink],
    channel_counts: Counter[str],
    sanctions_result: tuple[uuid.UUID, int] | None,
) -> None:
    n_accounts = sum(len(p.accounts) for p in persons)
    episodes = [a.episode for p in persons for a in p.accounts if a.episode is not None]
    treated = [e for e in episodes if e.treated]
    untreated = [e for e in episodes if not e.treated]
    link_counts = Counter(link.collision_type.value for link in links)

    typer.echo("")
    typer.echo("=== CONTINUUM data build summary ===")
    typer.echo(f"persons:                {len(persons)}")
    typer.echo(f"party_records:          {len(drafts)}")
    typer.echo(f"accounts:               {n_accounts}")
    typer.echo(f"delinquency episodes:   {len(episodes)}")
    typer.echo(f"  treated (contacted)   {len(treated)}")
    typer.echo(
        f"  observed cure rate    treated {_rate(treated):.3f}  untreated {_rate(untreated):.3f}"
    )
    ate = sum(e.p_cure_treated - e.p_cure_control for e in episodes) / max(len(episodes), 1)
    typer.echo(f"  true average effect   {ate:+.3f} (oracle, eval-only)")
    typer.echo(f"ground_truth_link:      {len(links)}")
    for collision_type, count in sorted(link_counts.items()):
        typer.echo(f"  {collision_type:<24} {count}")
    typer.echo(f"simulated contacts:     {sum(channel_counts.values())}")
    for channel, count in sorted(channel_counts.items()):
        typer.echo(f"  {channel:<8} {count}")
    if sanctions_result is not None:
        snapshot_id, n_listings = sanctions_result
        typer.echo(f"sanctions snapshot:     {snapshot_id} ({n_listings} listings)")
    else:
        typer.echo(f"sanctions snapshot:     skipped — {SANCTIONS_CSV} not found")
    typer.echo("")


if __name__ == "__main__":
    app()
