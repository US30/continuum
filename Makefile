.PHONY: install lint format typecheck test test-all check \
        db-up db-down db-reset migrate revision data-build scan \
        resolve resolve-metrics screen screen-metrics

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
