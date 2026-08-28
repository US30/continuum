.PHONY: install lint format typecheck test test-all check \
        db-up db-down db-reset migrate revision data-build scan

COMPOSE = docker compose -f docker/docker-compose.yml

install:
	uv sync --all-groups --all-extras
	uv run pre-commit install

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy continuum

test:
	uv run pytest -m "not integration"

test-all:
	uv run pytest

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
	uv run alembic upgrade head

revision:
	uv run alembic revision --autogenerate -m "$(m)"

# Phase 1 done-when: builds the full local dataset, re-runnable, prints row counts
# and the planted ground_truth_link summary.
data-build:
	uv run python -m continuum.foundation.build

scan:
	uv run gitleaks detect --no-git --redact --source .
