# Build from the repo root so gcs.py + requirements.txt are in context:
#   docker build -f reo-scraper/Dockerfile -t <image> .
FROM python:3.12-slim

# berglas binary resolves sm:// secret refs at runtime.
COPY --from=gcr.io/berglas/berglas:latest /bin/berglas /bin/berglas

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY gcs.py ./gcs.py
COPY reo-scraper/main.py reo-scraper/scraper_footage.py ./reo-scraper/

# berglas exec replaces env values like sm://PROJECT/name with the real secret,
# then runs the scraper. Credentials come from the Cloud Run service account (ADC).
ENTRYPOINT ["/bin/berglas", "exec", "--"]
CMD ["python", "-u", "reo-scraper/main.py"]
