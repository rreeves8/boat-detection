BUCKET := traffic-recordings
PUBLIC_BUCKET := boats-assets-boat-detection-509220
URL_MAP := boats-urlmap
CONTENT_TYPE := text/html; charset=utf-8
PORT := 8000

.DEFAULT_GOAL := deploy

.PHONY: deploy index data serve
# Both pages live in the public assets bucket: the CDN serves "/" and "/data"
# from it (see infra/cdn.tf); only the .mp4 clips stay gated in the private
# bucket. Deploying re-uploads and then invalidates the CDN cache for the pages.
deploy: index data

# Serve the UI locally: static files + the private bucket proxied with your
# gcloud credentials (so videos play). See ui/serve.py. Uses the repo venv,
# which has requests (gcs.py needs it); system python3 usually does not.
serve:
	PORT=$(PORT) .venv/bin/python ui/serve.py

index:
	gcloud storage cp ui/index.html gs://$(PUBLIC_BUCKET)/index.html \
		--content-type="$(CONTENT_TYPE)" --cache-control="no-cache"
	gcloud compute url-maps invalidate-cdn-cache $(URL_MAP) --path="/" --async

data:
	gcloud storage cp ui/data/index.html gs://$(PUBLIC_BUCKET)/data/index.html \
		--content-type="$(CONTENT_TYPE)" --cache-control="no-cache"
	gcloud compute url-maps invalidate-cdn-cache $(URL_MAP) --path="/data" --async
	gcloud compute url-maps invalidate-cdn-cache $(URL_MAP) --path="/data/" --async
