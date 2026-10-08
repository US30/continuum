# Secret Manager holds every secret; Cloud Run injects them as environment variables at start.
# Nothing secret is in the image, the repository, or a plain Terraform variable.

resource "google_secret_manager_secret" "postgres_password" {
  secret_id = "continuum-postgres-password"

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_version" "postgres_password" {
  secret      = google_secret_manager_secret.postgres_password.id
  secret_data = random_password.postgres.result
}

# HMAC key that signs guard verdicts (I2). Dispatch refuses any verdict it cannot verify.
resource "random_password" "guard_signing_key" {
  length  = 64
  special = false
}

resource "google_secret_manager_secret" "guard_signing_key" {
  secret_id = "continuum-guard-signing-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_version" "guard_signing_key" {
  secret      = google_secret_manager_secret.guard_signing_key.id
  secret_data = random_password.guard_signing_key.result
}

# Container only. The key itself is added by hand so it never passes through Terraform state:
#   printf '%s' "$KEY" | gcloud secrets versions add continuum-deepseek-api-key --data-file=-
# then set llm_enabled = true.
resource "google_secret_manager_secret" "deepseek_api_key" {
  secret_id = "continuum-deepseek-api-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}
