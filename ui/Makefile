BUCKET := traffic-recordings
CONTENT_TYPE := text/html; charset=utf-8
PORT := 8000

.DEFAULT_GOAL := deploy

.PHONY: deploy index data serve
deploy: index data

# Serve the UI locally: static files + the private bucket proxied with your
# gcloud credentials (so videos play). See serve.py.
serve:
	PORT=$(PORT) python3 serve.py

index:
	gcloud storage cp index.html gs://$(BUCKET)/index.html \
		--content-type="$(CONTENT_TYPE)" --cache-control="no-cache"

data:
	gcloud storage cp data/index.html gs://$(BUCKET)/data/index.html \
		--content-type="$(CONTENT_TYPE)" --cache-control="no-cache"
