provider "google" {
  project = "boat-detection-509220"
  region  = "us-east1"
}

# The bucket the scraper uploads Reolink recordings to. Private: read access is
# limited to the Cloudflare Worker's service account, the scraper job's account,
# and project owners. The public site is served by the Worker, not the bucket.
resource "google_storage_bucket" "recordings" {
  name          = "traffic-recordings"
  location      = "US-EAST1"
  project       = "boat-detection-509220"
  storage_class = "STANDARD"

  # Objects are addressed by name and read directly by the UI, so IAM (not ACLs)
  # governs access.
  uniform_bucket_level_access = true

  # GCS default: recover deleted objects for 7 days.
  soft_delete_policy {
    retention_duration_seconds = 604800
  }

  # Allow the local dev UI to range-request videos in the browser.
  cors {
    origin          = ["http://localhost:8000", "http://127.0.0.1:8000"]
    method          = ["GET", "HEAD"]
    response_header = ["Content-Type", "Content-Range", "Accept-Ranges", "ETag"]
    max_age_seconds = 3600
  }
}

# Read-only identity for the Cloudflare Worker. It authenticates to GCS with a
# key for this account (created out-of-band, set as a Worker secret) and is the
# only non-owner principal that can read objects. No public/allUsers access.
resource "google_service_account" "worker_reader" {
  account_id   = "cf-worker-reader"
  display_name = "Cloudflare Worker (read-only bucket access)"
}

resource "google_storage_bucket_iam_member" "worker_read" {
  bucket = google_storage_bucket.recordings.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.worker_reader.email}"
}
