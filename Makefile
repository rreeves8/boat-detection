BUCKET := traffic-recordings
PUBLIC_BUCKET := boats-assets-boat-detection-509220
CONTENT_TYPE := text/html; charset=utf-8
PORT := 8000

.DEFAULT_GOAL := deploy

.PHONY: deploy index data serve
# index.html + records.jsonl are public (the landing + charts); the viewer page
# is gated in the private bucket.
deploy: index data

# Serve the UI locally: static files + the private bucket proxied with your
# gcloud credentials (so videos play). See ui/serve.py. Uses the repo venv,
# which has requests (gcs.py needs it); system python3 usually does not.
serve:
	PORT=$(PORT) .venv/bin/python ui/serve.py

index:
	gcloud storage cp ui/index.html gs://$(PUBLIC_BUCKET)/index.html \
		--content-type="$(CONTENT_TYPE)" --cache-control="no-cache"

data:
	gcloud storage cp ui/data/index.html gs://$(BUCKET)/data/index.html \
		--content-type="$(CONTENT_TYPE)" --cache-control="no-cache"
