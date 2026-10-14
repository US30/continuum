# CONTINUUM

Agentic entity-resolved risk platform spanning the retail lending lifecycle, from KYC
screening to collections. Full build brief and invariants: [`CLAUDE.md`](./CLAUDE.md).

Lenders usually run onboarding due diligence and collections as separate systems. Two failures
follow: collections sees an **account**, not a **person**, so five delinquent accounts held by
one individual become five parallel dunning sequences; and compliance state goes stale, so a
customer who later appears on a sanctions list is still contacted.

CONTINUUM resolves identity and the one-hop network **once**, keeps it continuously screened,
and makes both halves of the lifecycle read the same risk file. The expensive thing to build —
knowing who this entity is and who they are connected to — is built once and used twice.

All data is synthetic. No real personal data is used anywhere (invariant I8).

## Status

| Phase | What | State |
|---|---|---|
| 0 | Skeleton: uv, ruff, mypy, pytest, docker-compose, Alembic, CI | Done |
| 1 | Data foundation: loans, SCD2 delinquency, planted identity collisions, contact-policy simulation, sanctions snapshot | Done |
| 2 | Entity resolution: blocking, Splink 4, stable clusters, one-hop network | Done |
| 3 | Screening: recall-first candidates, confirm / reject / adjudicate bands, LLM adjudicator | Done |
| 4 | Scoring: self-cure and response models, T-learner uplift, calibration | Done |
| 5 | Decisioning graph: LangGraph, six nodes, checkpointing, human-review interrupt | Done |
| 6 | Guard and dispatch: deterministic rules, tone check, signed verdicts, injection suite | Done |
| 7 | Evaluation harness: golden sets, audit stream, Metric 3, CI metric gate | Done |
| 8 | Deploy: Terraform, image, GitHub Actions rollout, review console | Deployed to GCP for the demo |

Phase 8 has been applied to a real GCP project. A push to `main` runs the CI gates, then
build, push, Terraform plan and apply, migrations, and a canary rollout. The nightly batch has
run in the cloud with the LLM adjudicator on. The Cloud Scheduler jobs are deployed **paused**,
so the batch is currently started by hand. See [`infra/README.md`](./infra/README.md).

## Architecture

**Nightly batch** (Prefect, Cloud Run job):

```
foundation.refresh   loan state + sanctions snapshot (atomic swap)
resolution.run       blocking -> Splink match -> cluster -> materialise network
screening.select     entities with a delinquency change in the last 24h, or matching new listings
screening.candidates loose fuzzy match; auto-confirm / auto-reject / adjudicate the middle band
audit.sample         random slice of auto-cleared entities re-run through the full pipeline
```

**Case decisioning** (LangGraph, per entity, Arq worker):

```
case_assembly -> scoring -> prioritise -> strategy -> draft -> guard -> send or escalate
```

Linked delinquent accounts collapse into one case at `case_assembly`. The guard is the only
egress point: deterministic rules (frequency caps, quiet hours, prohibited terms, hard block on
unresolved screening) run before any model call, so a prompt injection cannot unblock a send.

## The three metrics

Values below are the committed baseline in [`eval/baselines.json`](./eval/baselines.json),
computed locally on the synthetic dataset (seed `20260101`, 2,718 accounts). CI fails if a
fresh run regresses against them (`make ci-metrics`).

**1. Screening precision at 100% recall.** Recall is never traded away.

| Measure | Value |
|---|---|
| Labelled hard name pairs | 18 |
| Recall of the deterministic layer | 100% (0 true matches missed) |
| Share of candidates reaching the model | 77.8% |
| Planted sanctions hits false-cleared | 0 of 23 |

**2. Incremental recovery from uplift ranking vs. the rules baseline**, in currency, on 317
held-out episodes. The baseline is the simulated historical policy: contact at 30 DPD,
weighted by exposure (definition in `eval/uplift_metrics.py`).

| Measure | Value |
|---|---|
| Uplift ranking minus rules baseline | +11,431 (95% CI 5,801 to 17,115) |
| Uplift ranking minus a rules top-K-by-exposure policy | −2,788 |

The uplift ranking beats the historical policy but does **not** beat a simpler policy that
contacts the highest-exposure accounts with the same budget. That gap is open.

**3. Share of collections cases where the entity view beat the account view** — the headline.

| Measure | Value |
|---|---|
| Cases | 294 |
| Validated share | 9.9% |
| Raw share | 10.5% |
| Fraud-ring cases caught | 13 of 13 |
| Non-ring flag rate | 3.2% |

**Supporting checks:** resolution precision 1.000, recall 0.955, F1 0.977; prompt-injection
suite 0 of 47 succeeded; guard golden set 0 of 30 wrong.

**Cloud run (LLM on), not yet reflected above:** one `--full-screen` nightly batch adjudicated
300 candidates (the nightly cap) and deferred 1,769. The metrics job then reported **1 of 23**
planted hits false-cleared. The cause is not yet confirmed; the likely source is the LLM
adjudicator.

## Local setup

```bash
make install       # uv sync + pre-commit install
# start Docker Desktop
make db-up         # Postgres 17 (pgvector, pg_trgm) + Redis
make migrate       # apply Alembic migrations
make data-build    # build the synthetic dataset (Phase 1)
make check         # lint + typecheck + unit tests
```

Common targets after that:

```bash
make nightly       # run the whole nightly batch as one Prefect flow
make worker        # Arq worker for the decisioning graph
make cases         # queue a sample of cases
make api           # review console on http://localhost:8080
make metrics       # all three metrics plus supporting checks
make metrics-check # compare against eval/baselines.json
```

See the `Makefile` for the full target list.

## Non-negotiables

See CLAUDE.md §2 for the full list (I1–I8). Two are enforced by CI rather than convention:

- No provider SDK is imported outside `continuum/llm/`; every model call goes through LiteLLM.
- `ground_truth_link` (eval-only ground truth) is never imported by runtime code.

## Known limits

- When the sanctions listing changes, earlier screening decisions stop counting, and the
  nightly selection does not re-screen everyone. Run the batch with `--full-screen` after a
  listing change. A permanent policy is still to be decided.
- The nightly adjudication cap (`MAX_ADJUDICATIONS_PER_NIGHT`) defers the rest of the
  ambiguous band to later nights; deferred entities stay blocked from outreach.
- Models run through the DeepSeek API via LiteLLM, not self-hosted. Langfuse is not deployed;
  spans go to Cloud Trace only.
