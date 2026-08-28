# CONTINUUM — Build Brief

Agentic entity-resolved risk platform spanning the customer lifecycle from KYC screening to
collections. This file is the source of truth for what we are building and how. Read it fully
before writing code. If something here conflicts with a request, say so rather than silently
picking one.

---

## 1. What this is

Retail lenders run onboarding due diligence and delinquency collections as separate systems
that never share state. Two failures follow:

1. When a borrower defaults, collections sees an **account**, not a **person** — so five
   delinquent accounts controlled by one individual become five parallel dunning sequences
   instead of one coordinated case.
2. Compliance state goes stale. Someone who onboarded clean may appear on a sanctions list
   months later, and nothing in the collections path knows.

CONTINUUM resolves customer identity and network **once**, keeps it continuously screened, and
makes both halves of the lifecycle read the same risk file. Collections decisions are made on
the entity, not the account, and no outreach goes out to a customer whose compliance status is
unresolved.

**The one sentence that matters:** the expensive thing to build — knowing who this entity is
and who they are connected to — is built once and used twice.

---

## 2. Non-negotiable invariants

These are architectural commitments, not preferences. Do not violate them to make something
easier. If you think one is wrong, stop and raise it.

| # | Invariant |
|---|---|
| I1 | **No LLM in the deterministic layers.** Entity resolution, network materialisation, candidate generation, uplift scoring and case assembly contain zero model calls. Ever. |
| I2 | **The compliance guard is the single egress point.** No code path may dispatch an outbound message without a guard verdict. Deterministic rules run *before* any model call, so a prompt injection cannot unblock a send. |
| I3 | **`risk_file_event` is append-only.** Never `UPDATE` or `DELETE`. Current state is a view over the latest event per entity. |
| I4 | **`ground_truth_link` is invisible to runtime code.** Only the evaluation harness may read it. A runtime import of it is a build failure. |
| I5 | **Screening is recall-first.** Candidate generation is deliberately over-inclusive. Never tune it toward precision — precision is the adjudicator's job. |
| I6 | **Everything is idempotent.** Every batch job can be re-run on the same inputs without duplicating or corrupting state. Key on natural identifiers, not insertion order. |
| I7 | **Model choice is configuration.** All model calls go through the LiteLLM gateway. No provider SDK imported anywhere outside `continuum/llm/`. |
| I8 | **Synthetic data only.** No real personal data enters this repo or any environment, at any point. |

---

## 3. Scope

**In scope**

- Individual (retail) borrowers only
- Probabilistic entity resolution + one-hop network
- Sanctions/PEP screening with model adjudication of the ambiguous band only
- Nightly re-screening triggered by delinquency state change
- Self-cure and treatment-response models; uplift-based case ranking
- Outreach strategy, drafting, compliance guard
- Human review queue
- Cloud deployment, CI/CD, observability, evaluation harness

**Explicitly out of scope — do not build these, do not suggest building them**

- Corporate entities, beneficial ownership, multi-layer ownership traversal
- OCR / document parsing
- Adverse media ingestion
- Real-time streaming (re-screening is nightly batch **by design**, not as a limitation)
- Voice or chatbot channels — SMS and email only
- Payment processing, settlement negotiation, legal recovery
- Multi-region deployment

Scope creep is the main risk to this project. If a task drifts into the right-hand list,
stop and flag it.

---

## 4. Stack

```
Language      Python 3.12                 (uv for dependency management)
Orchestration LangGraph                   (agent runtime, checkpointing, interrupts)
Resolution    Splink 4                    (DuckDB backend local, Postgres in cloud)
ML            LightGBM, scikit-learn, Optuna
Database      PostgreSQL 17 + pgvector + pg_trgm   (Alembic migrations)
API           FastAPI + Pydantic v2
Workers       Arq on Redis
Batch         Prefect 3
LLM gateway   LiteLLM
Models        DeepSeek-V4        — prioritisation, strategy   (low volume)
              Llama 3.2 8B       — screening adjudication, guard tone check (main volume)
              Llama 3 8B 4-bit   — message drafting (cheapest tier)
Serving       Ollama or vLLM, self-hosted (PII residency)
Cloud         GCP — Cloud Run (services + jobs), Cloud SQL, Memorystore, Cloud Storage,
              Artifact Registry, Secret Manager
IaC / CI      Terraform, Docker, GitHub Actions
Observability OpenTelemetry GenAI semantic conventions → Cloud Trace; Langfuse self-hosted
Quality       ruff, mypy (strict on core packages), pytest
```

Do **not** introduce: Kafka (rejected — nightly batch plus a bounded queue does not need a
broker), Kubernetes (Cloud Run is sufficient at this scale), Neo4j (one-hop network is a
Postgres self-join), AutoGen (maintenance mode), LangChain agents (we use LangGraph directly).

---

## 5. Repository layout

```
continuum/
├── continuum/
│   ├── config/           # pydantic-settings, env loading, model routing table
│   ├── db/               # SQLAlchemy models, session, Alembic migrations
│   ├── foundation/       # loan load, delinquency state machine, synthetic identity,
│   │                     #   contact-policy simulation, sanctions ingest
│   ├── resolution/       # blocking, Splink model, clustering, network materialisation
│   ├── screening/        # candidate generation, band logic, adjudicator node
│   ├── scoring/          # self-cure model, response model, uplift, calibration
│   ├── graph/            # LangGraph: nodes, edges, state schema, checkpointer
│   │   └── nodes/        # case_assembly, prioritise, strategy, draft, guard, route
│   ├── guard/            # deterministic rules + tone check. NO other module sends.
│   ├── llm/              # LiteLLM client, routing, retries, circuit breaker
│   ├── api/              # FastAPI routers, schemas, auth, RLS session context
│   ├── workers/          # Arq task definitions
│   ├── flows/            # Prefect flows for nightly batch
│   └── telemetry/        # OTel setup, span helpers, cost accounting
├── eval/                 # golden sets, metric computation, audit stream, injection suite
├── tests/                # unit + integration (mirrors package layout)
├── infra/                # Terraform
├── data/                 # gitignored — local datasets only
├── docker/
├── Makefile
└── CLAUDE.md             # this file
```

---

## 6. Data model

Core tables. Full column detail lives in the Alembic migrations once written.

| Table | Key | Notes |
|---|---|---|
| `party_record` | PK `record_id` | Raw customer record as received. **Never mutated.** |
| `entity` | PK `entity_id` | Resolved person. Cluster size, confidence, resolution run id. |
| `entity_member` | PK (`entity_id`,`record_id`) | Cluster membership with match probability. |
| `entity_edge` | PK `edge_id` | One-hop link. `edge_type` ∈ {shared_address, shared_phone, name_similarity}, strength. Indexed both directions. Materialised nightly. |
| `account` | PK `account_id`, FK `entity_id` | Product, exposure, origination date, current state. |
| `payment_event` | PK `event_id`, FK `account_id` | Installment-level history. Feeds both propensity models. |
| `delinquency_state` | PK (`account_id`,`effective_from`) | SCD2. current / 30 / 60 / 90 DPD / cured / charged_off. **Transitions drive re-screening.** |
| `sanctions_entity` | PK `listing_id` | Snapshot-scoped reference listing. |
| `sanctions_alias` | PK `alias_id`, FK `listing_id` | Aliases + transliterations. Trigram indexed. |
| `screening_decision` | PK `decision_id`, FK `entity_id` | Outcome, method ∈ {auto_confirm, auto_reject, adjudicated, human}, evidence, model version. |
| `risk_file_event` | PK `event_id`, FK `entity_id` | **Append-only.** See I3. |
| `case` | PK `case_id`, FK `entity_id` | Spans ≥1 account. Rank, expected recovery, disposition. |
| `contact_event` | PK `contact_id`, FK `case_id` | Channel, timestamp, body hash, guard verdict, response. Enforces frequency caps. |
| `ground_truth_link` | PK `link_id` | Planted collisions. **Eval only — see I4.** |

Indexes that matter: trigram on `sanctions_alias.name` and `party_record.name`; composite
`(account_id, effective_from DESC)`; partial index on `risk_file_event` where the event is a
screening outcome (the guard's hot path).

---

## 7. The two runtime flows

**Nightly batch (Prefect)**

```
foundation.refresh          → loan state + sanctions snapshot (atomic swap)
  → resolution.run          → blocking → Splink match → cluster → materialise network
    → screening.select      → entities with delinquency state change in last 24h
    │                       + entities matching newly added sanctions entries
    → screening.candidates  → loose fuzzy match, high recall
      → auto_confirm / auto_reject written directly
      → ambiguous band → queue `screening.adjudicate`
    → audit.sample          → random slice of auto-cleared → full pipeline (false-clear rate)
```

**Case decisioning (LangGraph, per entity)**

```
case_assembly     entity + all accounts + one-hop network + risk file state
                  linked delinquent accounts COLLAPSE INTO ONE CASE here   ← the thesis
      ↓
scoring           self-cure P, response P, uplift = difference             (no LLM)
      ↓
prioritise        rank by expected recovery net of contact cost
                  network fraud signature → divert out of collections entirely
      ↓
strategy          channel + timing under frequency caps and quiet hours
      ↓
draft             tone-matched message
      ↓
guard             deterministic rules FIRST (frequency, quiet hours, prohibited
                  terms, hard block if risk file screening state unresolved)
                  then tone check
      ↓
send or escalate  cleared → dispatch queue; blocked or high-exposure → interrupt
```

Checkpoint at every node so an interrupted case resumes rather than restarts.

---

## 8. Build phases

Work through these in order. Each phase has a definition of done — do not move on until it
is met, and tell me when a phase completes rather than rolling straight into the next.

### Phase 0 — Skeleton (do this first)
Repo layout, `uv` project, ruff + mypy + pytest configured, Makefile, docker-compose with
Postgres 17 (pgvector, pg_trgm) and Redis, Alembic initialised, settings module, CI workflow
running lint + type + test on every PR.
**Done when:** `make check` passes on an empty test suite and `make db-up && make migrate`
gives a working local database.

### Phase 1 — Data foundation
Load loan and payment tables. Derive the delinquency state machine as SCD2. Attach synthetic
identity attributes. **Plant identity collisions and record them in `ground_truth_link`.**
Simulate a historical contact policy (rules-based: contact at 30 DPD weighted by exposure)
so the uplift models have a treatment variable. Ingest OpenSanctions to snapshot tables with
atomic swap.
**Done when:** a single command builds the full local dataset from scratch, is re-runnable
without duplication, and prints row counts plus a planted-link summary.

> The contact-policy simulation is the highest-risk item in the whole build. It is scheduled
> first deliberately. Do not defer it.

### Phase 2 — Entity resolution
Blocking rules, Splink model with per-attribute comparison levels (Jaro-Winkler on names,
exact-then-fuzzy on address, exact on identifiers), cluster assignment with stable ids,
one-hop network materialisation as a Postgres view.
**Done when:** resolution precision / recall / F1 against `ground_truth_link` is reported by
a single command, and cluster ids are stable across two consecutive runs on unchanged data.

### Phase 3 — Screening
Deterministic candidate generation (loose threshold, high recall). Band logic: auto-confirm
above ceiling, auto-reject below floor, adjudicate the middle. Adjudicator node via LiteLLM
returning structured `{verdict, confidence, reasoning}`. Append every outcome to the risk file.
Build the labelled hard-name-pair set (transliterations, common names, DOB near-misses).
**Done when:** **Metric 1** — precision at 100% recall on the labelled set — is reported, and
the proportion of candidates reaching the model is measured.

### Phase 4 — Scoring and uplift
Self-cure model (P of cure with no contact) and response model (P of response given contact).
Uplift = difference. Calibration with reliability curves and Brier score. Backtest against a
rules-based baseline with proper time separation.
**Done when:** **Metric 2** — incremental recovery versus the rules baseline, in currency — is
reported with the baseline definition written down.

### Phase 5 — Decisioning graph
LangGraph state schema, the six nodes, checkpointer on Redis, interrupt for human review.
Case assembly must collapse linked accounts. Prioritisation must read the network.
**Done when:** an end-to-end case runs from queue to guard verdict, resumes correctly after
an interrupt, and every node emits an OTel span with token count and cost.

### Phase 6 — Guard and dispatch
Deterministic rule engine, then tone check. Dispatch worker reachable only after a pass.
Prompt-injection suite: crafted free-text fields attempting to alter the verdict.
**Done when:** no injection in the suite succeeds, and a test proves dispatch is unreachable
without a verdict.

### Phase 7 — Evaluation harness and Metric 3
Golden sets, the randomised audit stream, and the network-value metric.
**Done when:** **Metric 3** — the share of collections cases where the entity view surfaced a
linked account, common controlling party or fraud signature the account view would have missed
— is reported, and CI fails on regression in any of the three metrics.

### Phase 8 — Deploy
Terraform for Cloud Run services and jobs, Cloud SQL private IP, Memorystore, Artifact
Registry, Secret Manager, Cloud Scheduler. GitHub Actions: build → push → plan → apply →
gradual rollout. Review console.
**Done when:** a push to trunk deploys, and the nightly batch runs on schedule in the cloud.

---

## 9. The three metrics

Everything is judged on these. They go in CI and they lead the final presentation.

1. **Screening precision at 100% recall** — you can never miss a true hit, so false positives
   are the only lever. Production systems often run above 95% false positives.
2. **Incremental recovery from uplift ranking vs. the rules baseline** — in currency, not AUC.
3. **Share of cases where the entity view beat the account view** — this is the number that
   justifies building the two halves as one system. It is the headline.

---

## 10. Conventions

- **Types everywhere.** Pydantic v2 models for all inter-node contracts. `mypy --strict` on
  `continuum/graph`, `continuum/guard`, `continuum/llm`.
- **Tests before implementation for deterministic modules** (resolution, screening bands,
  guard rules, state machine). These have exact expected outputs; write them first.
- **No magic thresholds.** Every threshold is a named setting with a comment explaining how it
  was chosen. If it was tuned, say against what.
- **Structured logging**, correlation id propagated across batch → queue → graph nodes. PII
  hashed at write.
- **Migrations are forward-only.** Never edit an applied migration.
- **Prompts live in files**, not string literals in logic, and are versioned. The version goes
  into `screening_decision.model_version`.
- **Commit style:** conventional commits, one logical change per commit.

---

## 11. How I want you to work

- Work one phase at a time. At the end of each phase, summarise what was built, what the
  definition of done showed, and what you would flag before moving on.
- **Ask before assuming** on anything touching the data model, the invariants, or scope.
- Do not invent data or fabricate metric values. If a number is not computed, say it is not
  computed.
- Prefer boring, obvious code. This will be read by a reviewer who has not seen it before.
- When you hit a genuine fork (library choice, threshold strategy, schema shape), present the
  options with trade-offs rather than silently choosing.
- If something in this file turns out to be wrong once you are in the code, tell me. Do not
  route around it.

---

## 12. Setup notes

- Datasets are downloaded manually into `data/` (gitignored) — the loan dataset requires an
  account, and the OpenSanctions bulk export is a direct download. Never commit them.
- Local models via Ollama; the LiteLLM routing table maps logical node names to model ids so
  a swap is a config change.
- Secrets via `.env` locally (gitignored, with `.env.example` committed) and Secret Manager
  in the cloud. No secret in the repo, image, or a commit — a pre-commit scanner enforces this.

**Start with Phase 0.** Confirm the layout and stack with me before writing application code.
