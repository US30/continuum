locals {
  # Plain configuration shared by every workload. Names match continuum.config.settings.
  env = {
    ENVIRONMENT                         = "production"
    LOG_LEVEL                           = "INFO"
    POSTGRES_HOST                       = google_sql_database_instance.main.private_ip_address
    POSTGRES_PORT                       = "5432"
    POSTGRES_DB                         = google_sql_database.continuum.name
    POSTGRES_USER                       = google_sql_user.continuum.name
    REDIS_HOST                          = google_redis_instance.queue.host
    REDIS_PORT                          = tostring(google_redis_instance.queue.port)
    CHECKPOINT_BACKEND                  = "postgres"
    LLM_ENABLED                         = tostring(var.llm_enabled)
    SCREENING_MAX_ADJUDICATIONS_PER_RUN = tostring(var.max_adjudications_per_night)
  }

  # Injected from Secret Manager. The model key is referenced only once it can exist.
  secret_env = merge(
    {
      POSTGRES_PASSWORD = google_secret_manager_secret.postgres_password.secret_id
      GUARD_SIGNING_KEY = google_secret_manager_secret.guard_signing_key.secret_id
    },
    var.llm_enabled ? { DEEPSEEK_API_KEY = google_secret_manager_secret.deepseek_api_key.secret_id } : {},
  )

  otel_collector_image = "us-docker.pkg.dev/cloud-ops-agents-artifacts/google-cloud-opentelemetry-collector/otelcol-google:0.160.0"

  run_depends_on = [
    google_secret_manager_secret_version.postgres_password,
    google_secret_manager_secret_version.guard_signing_key,
    google_secret_manager_secret_iam_member.runtime,
  ]
}

# ---- API: health check + review console -------------------------------------------------
# Identity-Aware Proxy sits in front: only the `reviewers` reach the container, and IAP tells
# the application who they are. The image and the traffic split belong to the deploy
# workflow (gradual rollout), so Terraform leaves them alone after creation.

resource "google_cloud_run_v2_service" "api" {
  name                = "continuum-api"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  iap_enabled         = true
  deletion_protection = false

  template {
    service_account = google_service_account.runtime.email

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = google_compute_network.main.id
        subnetwork = google_compute_subnetwork.run.id
      }
    }

    containers {
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      dynamic "env" {
        for_each = local.env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }

      # A revision that cannot answer /healthz never becomes ready, so it never takes traffic.
      startup_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds    = 5
        failure_threshold = 12
      }

      liveness_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds = 30
      }

      # Read-only: the dashboard reads the metrics report the metrics job writes here.
      volume_mounts {
        name       = "data"
        mount_path = "/app/data"
      }
    }

    volumes {
      name = "data"
      gcs {
        bucket    = google_storage_bucket.data.name
        read_only = true
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      traffic,
      client,
      client_version,
    ]
  }

  depends_on = [local.run_depends_on, google_storage_bucket_iam_member.runtime_data]
}

resource "google_project_service_identity" "iap" {
  provider = google-beta
  service  = "iap.googleapis.com"

  depends_on = [google_project_service.enabled]
}

resource "google_cloud_run_v2_service_iam_member" "iap_invokes_api" {
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_project_service_identity.iap.member
}

resource "google_iap_web_cloud_run_service_iam_member" "reviewers" {
  for_each = toset(var.reviewers)

  cloud_run_service_name = google_cloud_run_v2_service.api.name
  location               = var.region
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}

# ---- Worker pool: Arq worker running the decisioning graph and the guarded dispatch ------
# A pull worker with no HTTP port. One instance: the graph run is CPU-light and model calls
# dominate. The collector sidecar forwards node spans (token count, cost) to Cloud Trace.

resource "google_cloud_run_v2_worker_pool" "worker" {
  name                = "continuum-worker"
  location            = var.region
  deletion_protection = false

  scaling {
    manual_instance_count = 1
  }

  template {
    service_account = google_service_account.runtime.email

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = google_compute_network.main.id
        subnetwork = google_compute_subnetwork.run.id
      }
    }

    containers {
      name    = "worker"
      image   = var.image
      command = ["arq", "continuum.workers.tasks.WorkerSettings"]

      resources {
        limits = {
          cpu    = "1"
          memory = "2Gi"
        }
      }

      dynamic "env" {
        for_each = merge(local.env, { OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4317" })
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }

      volume_mounts {
        name       = "data"
        mount_path = "/app/data"
      }
    }

    containers {
      name  = "otel-collector"
      image = local.otel_collector_image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }

    volumes {
      name = "data"
      gcs {
        bucket    = google_storage_bucket.data.name
        read_only = true
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [local.run_depends_on, google_storage_bucket_iam_member.runtime_data]
}

# ---- Jobs ---------------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "migrate" {
  name                = "continuum-migrate"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.runtime.email
      max_retries     = 0
      timeout         = "600s"

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = google_compute_network.main.id
          subnetwork = google_compute_subnetwork.run.id
        }
      }

      containers {
        image   = var.image
        command = ["alembic", "upgrade", "head"]

        dynamic "env" {
          for_each = local.env
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = local.secret_env
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  depends_on = [local.run_depends_on]
}

# The nightly batch (CLAUDE.md §7), one Prefect flow, run to completion. Nightly by design.
resource "google_cloud_run_v2_job" "nightly" {
  name                = "continuum-nightly"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.runtime.email
      # Every stage is idempotent (I6), so one automatic retry of a failed night is safe.
      max_retries = 1
      timeout     = "3600s"

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = google_compute_network.main.id
          subnetwork = google_compute_subnetwork.run.id
        }
      }

      containers {
        image   = var.image
        command = ["python", "-m", "continuum.flows.nightly", "--n-persons", tostring(var.n_persons)]

        resources {
          limits = {
            cpu    = "2"
            memory = "4Gi"
          }
        }

        dynamic "env" {
          for_each = local.env
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = local.secret_env
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }

        volume_mounts {
          name       = "data"
          mount_path = "/app/data"
        }
      }

      volumes {
        name = "data"
        gcs {
          bucket    = google_storage_bucket.data.name
          read_only = false
        }
      }
    }
  }

  depends_on = [local.run_depends_on, google_storage_bucket_iam_member.runtime_data]
}

# Puts collections cases on the worker's queue and waits for them. Today this is the varied
# sample picker (continuum.workers.enqueue); it is not yet a full sweep of the delinquent book.
resource "google_cloud_run_v2_job" "cases" {
  name                = "continuum-cases"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.runtime.email
      max_retries     = 0
      timeout         = "3600s"

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = google_compute_network.main.id
          subnetwork = google_compute_subnetwork.run.id
        }
      }

      containers {
        image   = var.image
        command = ["python", "-m", "continuum.workers.enqueue", "--limit", "20"]

        dynamic "env" {
          for_each = local.env
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = local.secret_env
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  depends_on = [local.run_depends_on]
}

# The evaluation harness (CLAUDE.md §9): computes the three metrics against the cloud database
# and writes data/metrics_report.json to the bucket, where the dashboard reads it. It is the
# only workload that runs eval/, so it is the only one that reads ground truth (I4). Not
# scheduled: run it by hand after a nightly batch.
resource "google_cloud_run_v2_job" "metrics" {
  name                = "continuum-metrics"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.runtime.email
      max_retries     = 0
      timeout         = "1800s"

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = google_compute_network.main.id
          subnetwork = google_compute_subnetwork.run.id
        }
      }

      containers {
        image   = var.image
        command = ["python", "-m", "eval.metrics"]

        resources {
          limits = {
            cpu    = "2"
            memory = "4Gi"
          }
        }

        dynamic "env" {
          for_each = local.env
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = local.secret_env
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }

        volume_mounts {
          name       = "data"
          mount_path = "/app/data"
        }
      }

      volumes {
        name = "data"
        gcs {
          bucket    = google_storage_bucket.data.name
          read_only = false
        }
      }
    }
  }

  depends_on = [local.run_depends_on, google_storage_bucket_iam_member.runtime_data]
}
