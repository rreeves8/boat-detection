/* Commented out for now: the Cloud Run job needs its container image pushed
   before it can be created, which blocks a full apply. Re-enable once the
   image is in Artifact Registry.

resource "google_project_service" "apis" {
  for_each = toset([
    "artifactregistry.googleapis.com",
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# --- Image registry -----------------------------------------------------------

resource "google_artifact_registry_repository" "scraper" {
  location      = "us-east1"
  repository_id = "reo-scraper"
  format        = "DOCKER"
  description   = "Reolink scraper container images"

  depends_on = [google_project_service.apis]
}

# Repo is private (no public binding). Cloud Run pulls the image as its service
# agent, so grant that one agent read access — nothing else can pull.
resource "google_artifact_registry_repository_iam_member" "run_agent_pull" {
  location   = "us-east1"
  repository = google_artifact_registry_repository.scraper.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:service-736611078528@serverless-robot-prod.iam.gserviceaccount.com"
}

# --- Runtime identity ---------------------------------------------------------

resource "google_service_account" "scraper" {
  account_id   = "reo-scraper"
  display_name = "Reolink scraper Cloud Run Job"
}

# Push + list recordings (objects.create for uploads, objects.list for the
# GCS resume lookup). Scoped to just this bucket.
resource "google_storage_bucket_iam_member" "scraper_bucket" {
  bucket = google_storage_bucket.recordings.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.scraper.email}"
}

# berglas resolves sm:// refs at container start using this SA.
resource "google_project_iam_member" "scraper_secrets" {
  project = "boat-detection-509220"
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.scraper.email}"
}

# --- The job ------------------------------------------------------------------

resource "google_cloud_run_v2_job" "scraper" {
  name                = "reo-scraper"
  location            = "us-east1"
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.scraper.email
      timeout         = "3600s"
      max_retries     = 1

      containers {
        image = "us-east1-docker.pkg.dev/boat-detection-509220/reo-scraper/scraper:latest"

        # berglas swaps each sm:// ref for the real secret value before exec.
        env {
          name  = "REOLINK_EMAIL"
          value = "sm://boat-detection-509220/reolink-email"
        }
        env {
          name  = "REOLINK_PASSWORD"
          value = "sm://boat-detection-509220/reolink-password"
        }
        env {
          name  = "REOLINK_TOTP_SECRET"
          value = "sm://boat-detection-509220/reolink-totp-secret"
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi"
          }
        }
      }
    }
  }

  depends_on = [google_project_service.apis]
}

# --- Daily trigger at 09:00 ---------------------------------------------------

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  name     = google_cloud_run_v2_job.scraper.name
  location = "us-east1"
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scraper.email}"
}

# Let Cloud Scheduler mint OAuth tokens as the scraper SA.
resource "google_service_account_iam_member" "scheduler_token_creator" {
  service_account_id = google_service_account.scraper.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-736611078528@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
}

resource "google_cloud_scheduler_job" "daily" {
  name      = "reo-scraper-daily"
  region    = "us-east1"
  schedule  = "0 9 * * *"
  time_zone = "America/New_York"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/boat-detection-509220/locations/us-east1/jobs/reo-scraper:run"

    oauth_token {
      service_account_email = google_service_account.scraper.email
    }
  }

  depends_on = [google_project_service.apis]
}
*/
