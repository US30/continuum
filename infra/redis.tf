# Memorystore holds the Arq job queue and the review-queue index only. It has no Redis
# modules, which is why case checkpoints are in Postgres. Basic tier: a restart loses queued
# jobs, which the next scheduled run re-queues (every job is idempotent, I6).
#
# The application has no Redis AUTH/TLS settings yet, so access control here is the private
# network alone.

resource "google_redis_instance" "queue" {
  name               = "continuum-queue"
  tier               = "BASIC"
  memory_size_gb     = 1
  region             = var.region
  redis_version      = "REDIS_7_2"
  authorized_network = google_compute_network.main.id
  connect_mode       = "PRIVATE_SERVICE_ACCESS"

  depends_on = [google_service_networking_connection.managed_services]
}
