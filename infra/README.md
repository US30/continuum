# Infrastructure

Terraform for the whole cloud deployment: one GCP project, one region.

| Piece | Resource | Notes |
|---|---|---|
| Review console + health | Cloud Run service `continuum-api` | Behind Identity-Aware Proxy; only `reviewers` get in |
| Decisioning graph + dispatch | Cloud Run worker pool `continuum-worker` | Arq worker, plus an OpenTelemetry collector sidecar for Cloud Trace |
| Nightly batch | Cloud Run job `continuum-nightly` | Prefect flow, started by Cloud Scheduler |
| Case queueing | Cloud Run job `continuum-cases` | Started by Cloud Scheduler after the batch |
| Migrations | Cloud Run job `continuum-migrate` | `alembic upgrade head`, run by every deploy |
| Database | Cloud SQL PostgreSQL 17, private IP | Also stores case checkpoints |
| Queue | Memorystore for Redis | Arq queue and the review-queue index only |
| Files | Cloud Storage bucket | Dataset files and the model artifact, mounted at `/app/data` |
| Images | Artifact Registry `continuum` | |
| Secrets | Secret Manager | Database password, guard signing key, model API key |
| Deploy identity | Workload identity federation | GitHub Actions, no stored key |

## Status

`terraform validate` passes (`make tf-validate`). **This configuration has not been applied
to a real project.** Expect to adjust details the first time it meets the real APIs.

## Deviations from CLAUDE.md, agreed before writing

- **Case checkpoints are in Postgres, not Redis.** The LangGraph Redis checkpointer needs the
  RedisJSON and RediSearch modules, which Memorystore does not have. Local development still
  uses Redis (redis-stack); the backend is the `CHECKPOINT_BACKEND` setting.
- **Models are called through the DeepSeek API via LiteLLM**, not self-hosted. All data is
  synthetic (I8). Moving to a self-hosted model later is a routing-table change (I7).
- **Langfuse is not deployed.** Spans go to Cloud Trace only.

## First deploy (once, by hand)

The pipeline authenticates with resources this Terraform creates, so the first apply cannot
come from the pipeline.

```sh
gcloud auth application-default login
gcloud storage buckets create gs://<state-bucket> --location=<region> --uniform-bucket-level-access

cd infra
cp terraform.tfvars.example terraform.tfvars      # fill in
terraform init -backend-config="bucket=<state-bucket>"
terraform apply
```

This first apply uses a public placeholder image. The worker pool will not start cleanly
until a real image is deployed and the nightly batch has produced a model artifact.

Then set these GitHub repository **variables** (none of them is a secret):

| Variable | Value |
|---|---|
| `GCP_PROJECT_ID` | the project id |
| `GCP_REGION` | the region |
| `TF_STATE_BUCKET` | the state bucket name |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `terraform output -raw workload_identity_provider` |
| `GCP_DEPLOYER_SERVICE_ACCOUNT` | `terraform output -raw deployer_service_account` |
| `REVIEWERS` (optional) | JSON list, e.g. `["user:pat@example.com"]` |
| `LLM_ENABLED` (optional) | `true` once the model key exists (below) |

Push to the trunk. `.github/workflows/deploy.yml` then runs:
CI gates, build, push, plan, apply, migrate, gradual rollout.

After the first real deploy, run the batch once so the workers have data and a model:

```sh
gcloud run jobs execute continuum-nightly --region <region> --wait
```

## Turning the model on

The key is added by hand so it never passes through Terraform state:

```sh
printf '%s' "$DEEPSEEK_API_KEY" | gcloud secrets versions add continuum-deepseek-api-key --data-file=-
```

Then set `llm_enabled = true` (or the `LLM_ENABLED` repository variable) and deploy. Until
then every LLM node uses its deterministic fallback, and ambiguous screening candidates stay
undecided, which keeps outreach to those entities blocked.

## Rollout

The API revision is created with no traffic and must pass its `/healthz` startup probe. It
then takes 10% of traffic for two minutes; any error-level log from the new revision rolls
traffic back to the previous revision and fails the deploy. Otherwise it takes 100%.
Terraform deliberately ignores the API image and traffic split, which belong to the rollout.

The worker pool and the jobs switch to the new image in one step, after migrations.

## Known limits

- Memorystore has no AUTH or TLS configured; the private network is its only access control.
- The console trusts the identity header set by Identity-Aware Proxy and does not itself
  verify the signed IAP assertion.
- `continuum-cases` queues a small varied sample of delinquent entities, not the whole book.
- A changed sanctions listing gets a new snapshot id, which makes earlier screening decisions
  stop counting. The nightly selection does not yet re-screen everyone in that case; run the
  batch with `--full-screen` after a listing change.
