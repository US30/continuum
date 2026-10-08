# ---- Runtime identity: what the containers run as --------------------------------------

resource "google_service_account" "runtime" {
  account_id   = "continuum-runtime"
  display_name = "CONTINUUM workloads (API, worker, jobs)"
}

locals {
  runtime_secrets = {
    postgres_password = google_secret_manager_secret.postgres_password.id
    guard_signing_key = google_secret_manager_secret.guard_signing_key.id
    deepseek_api_key  = google_secret_manager_secret.deepseek_api_key.id
  }
}

resource "google_secret_manager_secret_iam_member" "runtime" {
  for_each = local.runtime_secrets

  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.runtime.member
}

resource "google_storage_bucket_iam_member" "runtime_data" {
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectUser"
  member = google_service_account.runtime.member
}

# Spans: the collector sidecar in the worker pool writes to Cloud Trace as this identity.
resource "google_project_iam_member" "runtime_trace" {
  for_each = toset(["roles/cloudtrace.agent", "roles/telemetry.tracesWriter"])

  project = var.project_id
  role    = each.value
  member  = google_service_account.runtime.member
}

# ---- Scheduler identity: may start the two scheduled jobs, nothing else ----------------

resource "google_service_account" "scheduler" {
  account_id   = "continuum-scheduler"
  display_name = "CONTINUUM Cloud Scheduler"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler" {
  for_each = {
    nightly = google_cloud_run_v2_job.nightly.name
    cases   = google_cloud_run_v2_job.cases.name
  }

  name     = each.value
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.scheduler.member
}

# ---- Deploy identity: GitHub Actions, keyless (workload identity federation) -----------

resource "google_service_account" "deployer" {
  account_id   = "continuum-deployer"
  display_name = "CONTINUUM GitHub Actions deploy"
}

locals {
  # What `terraform apply` of this directory plus the rollout steps need. Project-level
  # admin roles per service rather than Owner.
  deployer_roles = [
    "roles/artifactregistry.admin",
    "roles/cloudscheduler.admin",
    "roles/cloudsql.admin",
    "roles/compute.networkAdmin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser",
    "roles/iam.workloadIdentityPoolAdmin",
    "roles/iap.admin",
    "roles/logging.viewer",
    "roles/redis.admin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/run.admin",
    "roles/secretmanager.admin",
    "roles/servicenetworking.networksAdmin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/storage.admin",
  ]
}

resource "google_project_iam_member" "deployer" {
  for_each = toset(local.deployer_roles)

  project = var.project_id
  role    = each.value
  member  = google_service_account.deployer.member
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"

  depends_on = [google_project_service.enabled]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # Only this repository, and only its trunk branches, can obtain a token at all.
  attribute_condition = join(" && ", [
    "assertion.repository == \"${var.github_repository}\"",
    "assertion.ref in [${join(", ", [for b in var.deploy_branches : "\"refs/heads/${b}\""])}]",
  ])

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "deployer_github" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}
