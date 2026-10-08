output "api_url" {
  description = "Review console (behind Identity-Aware Proxy)."
  value       = google_cloud_run_v2_service.api.uri
}

output "image_repository" {
  description = "Push images here: <image_repository>/continuum:<tag>."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "workload_identity_provider" {
  description = "GitHub Actions variable GCP_WORKLOAD_IDENTITY_PROVIDER."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "deployer_service_account" {
  description = "GitHub Actions variable GCP_DEPLOYER_SERVICE_ACCOUNT."
  value       = google_service_account.deployer.email
}

output "data_bucket" {
  description = "Dataset files and the model artifact."
  value       = google_storage_bucket.data.name
}
