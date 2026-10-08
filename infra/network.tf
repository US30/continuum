# One private network. Cloud SQL and Memorystore have private addresses only; Cloud Run
# reaches them through Direct VPC egress on the subnet below (no connector to run).

resource "google_compute_network" "main" {
  name                    = "continuum"
  auto_create_subnetworks = false

  depends_on = [google_project_service.enabled]
}

resource "google_compute_subnetwork" "run" {
  name                     = "continuum-run"
  network                  = google_compute_network.main.id
  region                   = var.region
  ip_cidr_range            = "10.10.0.0/24"
  private_ip_google_access = true
}

# Address range handed to Google-managed services (Cloud SQL, Memorystore) via VPC peering.
resource "google_compute_global_address" "managed_services" {
  name          = "continuum-managed-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  network       = google_compute_network.main.id
}

resource "google_service_networking_connection" "managed_services" {
  network                 = google_compute_network.main.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.managed_services.name]

  depends_on = [google_project_service.enabled]
}
