.PHONY: install lint format typecheck test test-all check \
        db-up db-down db-reset migrate revision data-build scan \
        resolve resolve-metrics screen screen-metrics screen-metrics-llm train uplift-metrics worker cases injection guard-golden network-metrics audit metrics metrics-check metrics-baseline ci-metrics \
        nightly api image tf-validate

COMPOSE = docker compose -f docker/docker-compose.yml
RUN = conda run --no-capture-output -n continuum

install:
	conda run --no-capture-output -n continuum pip install -e . --group dev
	$(RUN) pre-commit install

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .

format:
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

typecheck:
	$(RUN) mypy continuum

test:
	$(RUN) pytest -m "not integration"

test-all:
	$(RUN) pytest

# Phase 0 done-when (part 1): lint + typecheck + unit tests all pass.
check: lint typecheck test

db-up:
	$(COMPOSE) up -d
	@echo "waiting for postgres/redis healthchecks..."
	@$(COMPOSE) ps

db-down:
	$(COMPOSE) down

db-reset:
	$(COMPOSE) down -v
	$(COMPOSE) up -d

# Phase 0 done-when (part 2): migrations apply cleanly to a fresh database.
migrate:
	$(RUN) alembic upgrade head

revision:
	$(RUN) alembic revision --autogenerate -m "$(m)"

# Phase 1 done-when: builds the full local dataset, re-runnable, prints row counts
# and the planted ground_truth_link summary.
data-build:
	$(RUN) python -m continuum.foundation.build

# Phase 2 done-when (part 1): blocking -> Splink -> stable clustering -> merge -> network.
resolve:
	$(RUN) python -m continuum.resolution.run

# Phase 2 done-when (part 2): precision/recall/F1 against ground_truth_link, one command.
resolve-metrics:
	$(RUN) python -m eval.resolution_metrics

# Phase 3 done-when (part 1): select -> candidates -> band -> (auto-decide | adjudicate).
screen:
	$(RUN) python -m continuum.screening.run

# Phase 3 done-when (part 2): precision at 100% recall on the hard-name-pair set, plus the
# proportion of candidates reaching the model.
screen-metrics:
	$(RUN) python -m eval.screening_metrics

scan:
	$(RUN) gitleaks detect --no-git --redact --source .

# Stage 2 of Metric 1: same set, but the middle band goes through the live LLM adjudicator
# (needs DEEPSEEK_API_KEY in .env).
screen-metrics-llm:
	$(RUN) python -m eval.screening_metrics --with-adjudicator

# Phase 4: build episodes, time-split, train self-cure/response T-learner, calibrate, report.
train:
	$(RUN) python -m continuum.scoring.train

# Phase 4 done-when: Metric 2 — incremental recovery of uplift ranking vs the rules baseline.
uplift-metrics:
	$(RUN) python -m eval.uplift_metrics

# Phase 5: the decisioning graph. Start the worker in one terminal, enqueue in another.
worker:
	$(RUN) arq continuum.workers.tasks.WorkerSettings

cases:
	$(RUN) python -m continuum.workers.enqueue --limit 20

# Phase 6 done-when: no prompt injection in the suite succeeds. Offline; exits 1 on any success.
injection:
	$(RUN) python -m eval.injection_suite

# Phase 7: golden sets, audit stream, Metric 3, one report, and the CI regression gate.
guard-golden:
	$(RUN) python -m eval.guard_golden

network-metrics:
	$(RUN) python -m eval.network_value

audit:
	$(RUN) python -m eval.audit

metrics:
	$(RUN) python -m eval.metrics

metrics-check:
	$(RUN) python -m eval.metrics --check

# Refresh eval/baselines.json from the CURRENT database. Explicit, reviewed, never automatic.
metrics-baseline:
	$(RUN) python -m eval.metrics --write-baseline

# What CI runs: a deterministic 2,000-person dataset end to end, then the gate.
ci-metrics:
	$(RUN) python -m continuum.foundation.build --n-persons 2000
	$(RUN) python -m continuum.resolution.run
	LLM_ENABLED=false $(RUN) python -m continuum.screening.run --no-adjudicate
	$(RUN) python -m continuum.scoring.train
	$(RUN) python -m eval.metrics --check

# Phase 8: the nightly batch as one Prefect flow (what the Cloud Run job runs), the review
# console, the deployable image, and an offline check of the Terraform in infra/.
nightly:
	$(RUN) python -m continuum.flows.nightly

# Review console on http://localhost:8080 (needs `make db-up`; a worker must be running for
# decisions to take effect).
api:
	$(RUN) python -m continuum.api

image:
	docker build -f docker/Dockerfile -t continuum:local .

TERRAFORM = docker run --rm -v "$(CURDIR)/infra:/w" -w /w hashicorp/terraform:1.16

# No cloud credentials needed: checks formatting and that the configuration is internally valid.
tf-validate:
	$(TERRAFORM) init -backend=false -input=false
	$(TERRAFORM) fmt -check -recursive
	$(TERRAFORM) validate
