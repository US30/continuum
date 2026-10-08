# Cloud Scheduler starts the two jobs by calling the Cloud Run Admin API as the scheduler
# service account. Re-screening is nightly batch by design (CLAUDE.md §3), not streaming.

locals {
  scheduled_jobs = {
    nightly = {
      job      = google_cloud_run_v2_job.nightly.name
      schedule = var.nightly_schedule
    }
    cases = {
      job      = google_cloud_run_v2_job.cases.name
      schedule = var.cases_schedule
    }
  }
}

resource "google_cloud_scheduler_job" "run" {
  for_each = local.scheduled_jobs

  name             = "continuum-${each.key}"
  region           = var.region
  schedule         = each.value.schedule
  time_zone        = var.schedule_time_zone
  attempt_deadline = "180s"

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${each.value.job}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [google_project_service.enabled, google_cloud_run_v2_job_iam_member.scheduler]
}
