# One image for every CONTINUUM workload. The command is set per Cloud Run resource (infra/run.tf):
#   api      python -m continuum.api                      (default below)
#   worker   arq continuum.workers.tasks.WorkerSettings
#   nightly  python -m continuum.flows.nightly
#   migrate  alembic upgrade head
# Build from the repository root:  docker build -f docker/Dockerfile -t continuum .

FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv==0.9.8

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --all-extras --no-install-project

COPY continuum ./continuum
COPY alembic.ini ./alembic.ini
RUN uv sync --frozen --no-dev --all-extras

FROM python:3.12-slim AS runtime

# libgomp1: OpenMP runtime LightGBM links against; not in the slim base image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 continuum
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/continuum /app/continuum
COPY --from=builder /app/alembic.ini /app/alembic.ini

# /app/data is where the batch writes its dataset files and the model artifact. In the cloud
# a Cloud Storage bucket is mounted there; locally it is a plain writable directory.
RUN mkdir -p /app/data && chown continuum:continuum /app/data

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PREFECT_HOME=/tmp/prefect
USER continuum

EXPOSE 8080
CMD ["python", "-m", "continuum.api"]
