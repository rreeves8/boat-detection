terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.40"
    }
  }
}

# Auth comes from CLOUDFLARE_API_TOKEN in the environment (never in state).
provider "cloudflare" {}

data "cloudflare_zone" "main" {
  name = "magnusreeves.com"
}

# The login Worker (deployed by wrangler — see edge/) lives on login.*, proxied
# through Cloudflare. It only mints the Cloud CDN signed cookie; its secrets stay
# in wrangler, not Terraform.
resource "cloudflare_workers_domain" "login" {
  account_id  = data.cloudflare_zone.main.account_id
  zone_id     = data.cloudflare_zone.main.id
  hostname    = "login.magnusreeves.com"
  service     = "boat-gate"
  environment = "production"
}

# boats.* points straight at the Google Cloud load balancer, DNS-only (grey
# cloud) so Cloud CDN — not Cloudflare — serves it and the managed cert can
# validate. Cloudflare here is just DNS for this record.
resource "cloudflare_record" "boats" {
  zone_id = data.cloudflare_zone.main.id
  name    = "boats"
  type    = "A"
  value   = google_compute_global_address.cdn.address
  proxied = false
  ttl     = 300
}
