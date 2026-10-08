# CONTINUUM

Agentic entity-resolved risk platform spanning the customer lifecycle from KYC screening to
collections. Full build brief and invariants: [`CLAUDE.md`](./CLAUDE.md).

The expensive thing to build — knowing who this entity is and who they are connected to — is
built once (entity resolution + network) and used twice: to keep compliance screening current,
and to make collections decisions on the entity, not the account.

## Status

Phases 0-7 are built and run locally: data foundation, entity resolution, screening, uplift
scoring, the decisioning graph, guard and dispatch, and the evaluation harness with its CI
metric gate. Phase 8 (deploy) is written and checked offline — Terraform validates, the image
builds and runs the nightly batch, worker and review console locally — but **nothing has been
deployed to a cloud project yet**. See `infra/README.md`.

## Local setup

```bash
make install                 # uv sync + pre-commit install
# start Docker Desktop
make db-up                   # Postgres 17 (pgvector, pg_trgm) + Redis
make migrate                 # apply Alembic migrations
make data-build               # build the synthetic dataset (Phase 1)
make check                   # lint + typecheck + unit tests
```

See `Makefile` for the full target list.

## The three metrics (CLAUDE.md §9)

1. **Screening precision at 100% recall** — false positives are the only lever; recall is
   never traded away.
2. **Incremental recovery from uplift ranking vs. a rules baseline** — reported in currency.
3. **Share of collections cases where the entity view beat the account view** — the headline
   number justifying the whole architecture.

## Non-negotiables

See CLAUDE.md §2 for the full list (I1–I8). The two enforced by CI rather than by convention:

- No LLM call anywhere outside `continuum/llm/` reaching a provider SDK directly.
- `ground_truth_link` (eval-only ground truth) is never imported by runtime code.
