"""Phase 1 entrypoint: builds the full local dataset from scratch.

Run via `make data-build` (`python -m continuum.foundation.build`). Idempotent (CLAUDE.md
I6): every writer upserts on a natural key — party_record on (source_system,
source_record_id), entity on a person_id-derived id, etc. — so re-running with the same
DATA_SEED changes no row counts.

Architecture note — flagged, not silently resolved (CLAUDE.md §11: "ask before assuming
on anything touching the data model"): CLAUDE.md §6 gives `account` a NOT NULL FK straight
to `entity`, but entity resolution (Phase 2) does not exist yet. This loader therefore
creates one *singleton* Entity per synthetic person at load time (cluster_size=1,
confidence=1.0, a fixed sentinel resolution_run_id) and an entity_member row for every
party_record pointing at it, so accounts have somewhere valid to point today. Phase 2's
job then becomes *merging* these singletons (updating account.entity_id and retiring the
absorbed singleton) rather than creating entities from nothing. Confirm this reading before
Phase 2 starts — the alternative is adding a record_id FK to `account` and deferring
entity assignment entirely to Phase 2, which is a schema change CLAUDE.md doesn't describe.
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
from continuum.db.models.entity import Entity
from continuum.db.models.eval import GroundTruthLink
from continuum.db.models.party import PartyRecord
from continuum.db.models.screening import SanctionsAlias, SanctionsEntity
from continuum.db.session import session_scope
from continuum.foundation import collisions, contact_policy, delinquency, generate, sanctions
from continuum.telemetry.logging import configure_logging, get_logger

app = typer.Typer(add_completion=False)
logger = get_logger(__name__)

DEFAULT_N_PERSONS = 5_000
SANCTIONS_CSV = Path("data") / "targets.simple.csv"

# Fixed sentinel for the Phase-1 singleton entities described above — never used once
# Phase 2's real resolution_run_id values exist, kept constant so re-runs are idempotent.
SINGLETON_RESOLUTION_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _singleton_entity_id(person_id: uuid.UUID) -> uuid.UUID:
    """Deterministic entity id for a person's Phase-1 singleton entity (CLAUDE.md I6)."""
    return uuid.uuid5(person_id, "phase1-singleton")


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


def _load_singleton_entities_and_accounts(
    session: Session, persons: list[generate.SyntheticPerson], drafts: list[collisions.PartyRecordDraft]
) -> None:
    records_by_person: dict[uuid.UUID, list[collisions.PartyRecordDraft]] = {}
    for draft in drafts:
        records_by_person.setdefault(draft.person_id, []).append(draft)

    for person in persons:
        entity_id = _singleton_entity_id(person.person_id)
        person_records = records_by_person.get(person.person_id, [])

        entity_stmt = pg_insert(Entity).values(
            entity_id=entity_id,
            cluster_size=len(person_records),
            confidence=Decimal("1.0"),
            resolution_run_id=SINGLETON_RESOLUTION_RUN_ID,
        )
        entity_stmt = entity_stmt.on_conflict_do_update(
            index_elements=["entity_id"],
            set_={"cluster_size": entity_stmt.excluded.cluster_size},
        )
        session.execute(entity_stmt)

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
    session: Session, persons: list[generate.SyntheticPerson], seed: int
) -> Counter[str]:
    settings = get_settings()
    rng = random.Random(seed)
    channel_counts: Counter[str] = Counter()

    for person in persons:
        entity_id = _singleton_entity_id(person.person_id)
        for account in person.accounts:
            states = delinquency.derive_delinquency_states(
                account.payments, account.origination_date, generate.AS_OF_DATE
            )
            simulated = contact_policy.simulate_contacts(
                entity_id, account.account_id, float(account.exposure), states, settings, rng
            )
            for contact in simulated:
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

    for listing in listings:
        session.add(
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
        )
        all_names = [listing.primary_name, *listing.aliases]
        for name in all_names:
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
    n_persons: int = typer.Option(DEFAULT_N_PERSONS, help="Number of synthetic persons to generate."),
    seed: int | None = typer.Option(None, help="Override Settings.data_seed for this run."),
) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    effective_seed = seed if seed is not None else settings.data_seed

    logger.info("data_build.start", n_persons=n_persons, seed=effective_seed)

    persons = generate.generate_population(n_persons, effective_seed)
    drafts, links = collisions.build_party_records(persons, effective_seed)

    with session_scope() as session:
        _load_party_records_and_links(session, drafts, links)
        _load_singleton_entities_and_accounts(session, persons, drafts)
        channel_counts = _load_contact_history(session, persons, effective_seed)
        sanctions_result = _load_sanctions_snapshot(session, SANCTIONS_CSV)

    _print_summary(persons, drafts, links, channel_counts, sanctions_result)


def _print_summary(
    persons: list[generate.SyntheticPerson],
    drafts: list[collisions.PartyRecordDraft],
    links: list[collisions.PlantedLink],
    channel_counts: Counter[str],
    sanctions_result: tuple[uuid.UUID, int] | None,
) -> None:
    n_accounts = sum(len(p.accounts) for p in persons)
    profile_counts = Counter(a.profile.value for p in persons for a in p.accounts)
    link_counts = Counter(link.collision_type.value for link in links)

    typer.echo("")
    typer.echo("=== CONTINUUM data build summary ===")
    typer.echo(f"persons:                {len(persons)}")
    typer.echo(f"party_records:          {len(drafts)}")
    typer.echo(f"accounts:               {n_accounts}")
    for profile, count in sorted(profile_counts.items()):
        typer.echo(f"  profile {profile:<16} {count}")
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
