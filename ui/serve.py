#!/usr/bin/env python3
"""Local dev server for the footage UI.

Serves this directory (ui/) statically, and proxies the *private* GCS bucket
using YOUR gcloud credentials — so videos play locally with no Cloud CDN cookie,
exactly like production but authenticated as you.

    python3 serve.py            # http://localhost:8000
    PORT=9000 python3 serve.py

Requirements: gcloud logged in with access to the bucket (you, the owner).
Only the .mp4 clips come from GCS now; the analysis records are read straight
from Supabase by the browser (see the SUPABASE_URL config in the UI pages).

The authenticated GCS reads reuse the shared ``GCS`` client in ../gcs.py, so
the token handling and Range-aware object fetch live in exactly one place.
"""

import os
import sys
import urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gcs import GCS  # noqa: E402

BUCKET = "traffic-recordings"  # private: source .mp4 clips
UI_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8000"))
PROXY_SUFFIXES = (".mp4",)  # paths fetched from the bucket, not the filesystem

gcs = GCS(BUCKET)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=UI_DIR, **kwargs)

    def do_GET(self):
        name = urllib.parse.unquote(self.path.split("?", 1)[0].lstrip("/"))
        if name.endswith(PROXY_SUFFIXES):
            return self._proxy(name)
        return super().do_GET()

    def _proxy(self, name: str):
        with gcs.open_video(name, byte_range=self.headers.get("Range")) as upstream:
            self.send_response(upstream.status_code)
            for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                if upstream.headers.get(header):
                    self.send_header(header, upstream.headers[header])
            self.end_headers()
            for chunk in upstream.iter_content(262144):
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break


if __name__ == "__main__":
    print(f"Overview:  http://localhost:{PORT}/")
    print(f"Footage:   http://localhost:{PORT}/data/  (videos proxied from gs://{BUCKET} as you)")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
