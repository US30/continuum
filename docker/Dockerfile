# Multi-stage build for the CONTINUUM application image.
# Not used until Phase 8 (Cloud Run deploy) — written now so the shape exists.

FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project

COPY continuum ./continuum
COPY alembic.ini ./alembic.ini
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime

RUN useradd --create-home --uid 1000 continuum
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/continuum /app/continuum
COPY --from=builder /app/alembic.ini /app/alembic.ini

ENV PATH="/app/.venv/bin:${PATH}"
USER continuum

# Overridden per Cloud Run service/job — this is a placeholder entrypoint.
CMD ["python", "-m", "continuum.api"]
