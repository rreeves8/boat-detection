#!/usr/bin/env python3
"""Local dev server for the footage UI.

Serves this directory (ui/) statically, and proxies the *private* GCS bucket
using YOUR gcloud credentials — so videos play locally with no Cloud CDN cookie,
exactly like production but authenticated as you.

    python3 serve.py            # http://localhost:8000
    PORT=9000 python3 serve.py

Requirements: gcloud logged in with access to the bucket (you, the owner).
records.jsonl is served from the local file (ui/records.jsonl -> counting/…),
so you see your local analysis; only the .mp4s come from the bucket.
"""

import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer, SimpleHTTPRequestHandler
from threading import Lock

BUCKET = "traffic-recordings"
UI_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8000"))
PROXY_SUFFIXES = (".mp4",)  # paths fetched from the bucket, not the filesystem

_token = {"value": None, "exp": 0.0}
_token_lock = Lock()


def access_token() -> str:
    with _token_lock:
        if _token["value"] and _token["exp"] > time.time():
            return _token["value"]
        out = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        _token["value"], _token["exp"] = out, time.time() + 3000
        return out


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=UI_DIR, **kwargs)

    def do_GET(self):
        name = urllib.parse.unquote(self.path.split("?", 1)[0].lstrip("/"))
        if name.endswith(PROXY_SUFFIXES):
            return self._proxy(name)
        return super().do_GET()

    def _proxy(self, name: str):
        url = (
            f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/"
            f"{urllib.parse.quote(name, safe='')}?alt=media"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token()}"})
        if self.headers.get("Range"):
            req.add_header("Range", self.headers["Range"])
        try:
            upstream = urllib.request.urlopen(req)
        except urllib.error.HTTPError as exc:
            self.send_error(exc.code, exc.reason)
            return
        self.send_response(upstream.status)
        for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            if upstream.headers.get(header):
                self.send_header(header, upstream.headers[header])
        self.end_headers()
        while chunk := upstream.read(262144):
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break


if __name__ == "__main__":
    print(f"Overview:  http://localhost:{PORT}/")
    print(f"Footage:   http://localhost:{PORT}/data/  (videos proxied from gs://{BUCKET} as you)")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
