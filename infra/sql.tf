# PostgreSQL 17, private IP only. pgvector and pg_trgm are created by the first Alembic
# migration (CREATE EXTENSION IF NOT EXISTS), which the application user is allowed to run.
# The same database also holds the LangGraph case checkpoints (CHECKPOINT_BACKEND=postgres).

resource "google_sql_database_instance" "main" {
  name                = "continuum"
  database_version    = "POSTGRES_17"
  region              = var.region
  deletion_protection = var.deletion_protection

  settings {
    edition           = "ENTERPRISE"
    tier              = var.db_tier
    availability_type = "ZONAL"
    disk_autoresize   = true

    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.main.id
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }
  }

  depends_on = [google_service_networking_connection.managed_services]
}

resource "google_sql_database" "continuum" {
  name     = "continuum"
  instance = google_sql_database_instance.main.name
}

# Letters and digits only: the password is placed inside a connection URL by the application.
resource "random_password" "postgres" {
  length  = 32
  special = false
}

resource "google_sql_user" "continuum" {
  name     = "continuum"
  instance = google_sql_database_instance.main.name
  password = random_password.postgres.result
}
