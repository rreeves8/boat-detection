# Google Cloud CDN in front of the private bucket — the CloudFront+S3+OAC model.
# Cloud CDN's cache-fill agent reads the private bucket directly (no proxy);
# gated paths require a Cloud CDN signed cookie, minted by the login service.

variable "signed_url_key" {
  type        = string
  sensitive   = true
  description = "Base64url 16-byte key for Cloud CDN signed cookies (also given to the login service). Supply via TF_VAR_signed_url_key."
}

resource "google_project_service" "compute" {
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}

# Anycast IP + Google-managed cert for the HTTPS load balancer.
resource "google_compute_global_address" "cdn" {
  name       = "boats-ip"
  depends_on = [google_project_service.compute]
}

resource "google_compute_managed_ssl_certificate" "cdn" {
  name = "boats-cert"
  managed {
    domains = ["boats.magnusreeves.com"]
  }
}

# Small PUBLIC bucket for the login page shown on 403 (an unsigned backend can
# only serve a genuinely public bucket, so this can't live in the private one).
resource "google_storage_bucket" "assets" {
  name                        = "boats-assets-boat-detection-509220"
  location                    = "US-EAST1"
  project                     = "boat-detection-509220"
  uniform_bucket_level_access = true
}

resource "google_storage_bucket_iam_member" "assets_public" {
  bucket = google_storage_bucket.assets.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

resource "google_compute_backend_bucket" "public" {
  name        = "boats-public"
  bucket_name = google_storage_bucket.assets.name
  enable_cdn  = true
}

# Gated content (/data + videos): a signed-URL key makes signatures REQUIRED, so
# requests without a valid signed cookie are rejected at the edge.
resource "google_compute_backend_bucket" "gated" {
  name        = "boats-gated"
  bucket_name = google_storage_bucket.recordings.name
  enable_cdn  = true

  cdn_policy {
    signed_url_cache_max_age_sec = 3600
  }
}

resource "google_compute_backend_bucket_signed_url_key" "gate" {
  name           = "gate-key"
  backend_bucket = google_compute_backend_bucket.gated.name
  key_value      = var.signed_url_key
}

# The OAC analog: let Cloud CDN's cache-fill agent read the private bucket.
resource "google_storage_bucket_iam_member" "cdn_fill" {
  bucket = google_storage_bucket.recordings.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:service-736611078528@cloud-cdn-fill.iam.gserviceaccount.com"
}

# Routing: gated by default; only the landing + records.jsonl are public. Videos
# live at the bucket root, so leaving them on the default (gated) backend covers
# them without per-extension rules.
resource "google_compute_url_map" "cdn" {
  name            = "boats-urlmap"
  default_service = google_compute_backend_bucket.gated.id

  host_rule {
    hosts        = ["boats.magnusreeves.com"]
    path_matcher = "main"
  }

  # Public landing + records come from the public assets bucket (with the login
  # form on the page); /data + videos stay on the signed backend over the
  # private bucket and require the Cloud CDN cookie.
  path_matcher {
    name            = "main"
    default_service = google_compute_backend_bucket.gated.id

    # Public landing "/" -> index.html in the public assets bucket.
    path_rule {
      paths   = ["/"]
      service = google_compute_backend_bucket.public.id
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/index.html"
        }
      }
    }

    # Public analysis feed for the landing charts.
    path_rule {
      paths   = ["/records.jsonl"]
      service = google_compute_backend_bucket.public.id
    }

    # Public viewer page (catalog is derived from the public records.jsonl, so it
    # leaks nothing new). Only the video files below stay gated.
    path_rule {
      paths   = ["/data", "/data/"]
      service = google_compute_backend_bucket.public.id
      route_action {
        url_rewrite {
          path_prefix_rewrite = "/data/index.html"
        }
      }
    }
  }
}

resource "google_compute_target_https_proxy" "cdn" {
  name             = "boats-proxy"
  url_map          = google_compute_url_map.cdn.id
  ssl_certificates = [google_compute_managed_ssl_certificate.cdn.id]
}

resource "google_compute_global_forwarding_rule" "cdn" {
  name       = "boats-fr"
  target     = google_compute_target_https_proxy.cdn.id
  port_range = "443"
  ip_address = google_compute_global_address.cdn.address
}
