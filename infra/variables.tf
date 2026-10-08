variable "project_id" {
  description = "GCP project that holds every CONTINUUM resource (single project, single region)."
  type        = string
}

variable "region" {
  description = "The one region everything runs in. Multi-region is out of scope (CLAUDE.md §3)."
  type        = string
  default     = "us-central1"
}

variable "image" {
  description = <<-EOT
    Container image for the jobs, and the first image for the API service and worker pool.
    The deploy workflow passes the freshly built tag. The default is Google's public sample
    image so the very first `terraform apply` can succeed before any image has been pushed.
  EOT
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "github_repository" {
  description = "GitHub repository allowed to deploy, as owner/name."
  type        = string
}

variable "deploy_branches" {
  description = "Branches whose pushes may deploy (the trunk)."
  type        = list(string)
  default     = ["main", "trunk"]
}

variable "reviewers" {
  description = "IAM members allowed to open the review console, e.g. [\"user:pat@example.com\"]."
  type        = list(string)
  default     = []
}

variable "llm_enabled" {
  description = <<-EOT
    Whether workloads call the model. Leave false until a DeepSeek key has been added as a
    version of the deepseek-api-key secret: Cloud Run refuses to start a container that
    references a secret with no version. With false, every LLM node uses its deterministic
    fallback and ambiguous screening candidates stay undecided (outreach stays blocked).
  EOT
  type        = bool
  default     = false
}

variable "max_adjudications_per_night" {
  description = "Spend guard: cap on paid screening-adjudicator calls per nightly run."
  type        = number
  default     = 500
}

variable "n_persons" {
  description = "Size of the synthetic book the nightly batch (re)builds (synthetic data only, I8)."
  type        = number
  default     = 2000
}

variable "nightly_schedule" {
  description = "Cron for the nightly batch."
  type        = string
  default     = "0 2 * * *"
}

variable "cases_schedule" {
  description = "Cron for queueing collections cases. Must leave the nightly batch time to finish."
  type        = string
  default     = "0 4 * * *"
}

variable "schedule_time_zone" {
  description = "Time zone the two schedules are written in."
  type        = string
  default     = "Etc/UTC"
}

variable "db_tier" {
  description = "Cloud SQL machine tier."
  type        = string
  default     = "db-custom-1-3840"
}

variable "deletion_protection" {
  description = "Guards the database and data bucket against `terraform destroy`."
  type        = bool
  default     = true
}
