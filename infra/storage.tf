# Mounted at /app/data in the nightly job (read-write) and the worker pool (read-only).
# Holds the generated synthetic dataset files and the trained model artifact. Synthetic data
# only (I8). Versioned so a bad model artifact can be rolled back.

resource "google_storage_bucket" "data" {
  name                        = "${var.project_id}-continuum-data"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = !var.deletion_protection

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 7
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_artifact_registry_repository" "images" {
  repository_id = "continuum"
  location      = var.region
  format        = "DOCKER"

  depends_on = [google_project_service.enabled]
}
