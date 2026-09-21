"""Upload byte streams to GCS using the active gcloud CLI account."""

import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from typing import Iterable, Any
from urllib.parse import quote

import requests


class GCS:
    def __init__(self, bucket: str, chunk_size: int = 8 * 1024 * 1024):
        if not bucket or '/' in bucket:
            raise ValueError('bucket must be a bucket name, without gs:// or a path')
        if chunk_size <= 0 or chunk_size % (256 * 1024):
            raise ValueError('chunk_size must be a positive multiple of 256 KiB')
        self.bucket = bucket
        self.chunk_size = chunk_size
        self.session = requests.Session()
        self._token = None
        self._auth_lock = threading.Lock()
        self._expiry = datetime.min.replace(tzinfo=timezone.utc)

    def access_token(self, refresh: bool = False) -> str:
        """Cache credentials in memory until shortly before their actual expiry."""
        if refresh or not self._token or datetime.now(timezone.utc) + timedelta(minutes=1) >= self._expiry:
            self._token, self._expiry = self._adc_token() or self._gcloud_token()
        return self._token

    def _adc_token(self) -> tuple[str, datetime] | None:
        """Token via Application Default Credentials.

        Covers Cloud Run (the job's service account, via the metadata server)
        and local runs (``gcloud auth application-default login``). Returns None
        if google-auth is unavailable or no ADC is configured, so the caller can
        fall back to the gcloud CLI.
        """
        try:
            import google.auth
            import google.auth.transport.requests
            credentials, _ = google.auth.default(
                scopes=['https://www.googleapis.com/auth/devstorage.read_write'],
            )
            credentials.refresh(google.auth.transport.requests.Request())
        except Exception:
            return None
        if not credentials.token:
            return None
        expiry = credentials.expiry
        if expiry is None:
            expiry = datetime.now(timezone.utc) + timedelta(minutes=55)
        elif expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return credentials.token, expiry

    def _gcloud_token(self) -> tuple[str, datetime]:
        """Token via the gcloud CLI, for environments with only a user login."""
        try:
            result = subprocess.run(
                ['gcloud', 'config', 'config-helper', '--force-auth-refresh', '--format=json'],
                check=True, capture_output=True, text=True, timeout=60,
            )
            credential = json.loads(result.stdout)['credential']
            token = credential['access_token']
            expiry = datetime.fromisoformat(credential['token_expiry'].replace('Z', '+00:00'))
            if not token or expiry <= datetime.now(timezone.utc):
                raise ValueError('expired or empty credential')
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError('Could not get Google credentials; configure ADC or run gcloud auth login') from exc
        return token, expiry

    def upload_stream(self, name: str, chunks: Iterable[bytes], *, size: int | None = None,
                      content_type: str = 'video/mp4', overwrite: bool = False) -> dict[str, Any]:
        """Upload with bounded memory. Failures can be retried by rerunning the scraper."""
        if not name or (size is not None and size < 0):
            raise ValueError('name must be nonempty and size must be nonnegative')
        params = {'uploadType': 'resumable', 'name': name}
        if not overwrite:
            params['ifGenerationMatch'] = '0'
        for attempt in range(2):
            with self.session.post(
                f'https://storage.googleapis.com/upload/storage/v1/b/{quote(self.bucket, safe="")}/o',
                params=params,
                headers={'Authorization': f'Bearer {self.access_token(refresh=bool(attempt))}',
                         'X-Upload-Content-Type': content_type},
                json={'contentType': content_type}, timeout=30,
            ) as response:
                if response.status_code == 401 and attempt == 0:
                    continue
                response.raise_for_status()
                upload_url = response.headers['Location']
                break

        offset = 0
        buffer = bytearray()

        def send(data: bytes, final: bool) -> dict[str, Any] | None:
            nonlocal offset
            total = str(offset + len(data)) if final else '*'
            byte_range = f'{offset}-{offset + len(data) - 1}' if data else '*'
            with self.session.put(
                upload_url, data=data,
                headers={'Content-Type': content_type,
                         'Content-Range': f'bytes {byte_range}/{total}'},
                timeout=120, allow_redirects=False,
            ) as response:
                if final:
                    response.raise_for_status()
                    if response.status_code not in (200, 201):
                        raise RuntimeError('GCS did not finalize the upload')
                    return response.json()
                if response.status_code != 308:
                    response.raise_for_status()
                    raise RuntimeError('Unexpected GCS upload response')
                expected = offset + len(data)
                if response.headers.get('Range') != f'bytes=0-{expected - 1}':
                    raise RuntimeError('GCS did not acknowledge the complete chunk; rerun the upload')
                offset = expected
            return None

        for chunk in chunks:
            # Slice large source chunks too, so the upload buffer stays bounded.
            view = memoryview(chunk)
            while view:
                if len(buffer) == self.chunk_size:
                    send(bytes(buffer), False)
                    buffer.clear()
                count = min(self.chunk_size - len(buffer), len(view))
                buffer.extend(view[:count])
                view = view[count:]
                if size is not None and offset + len(buffer) > size:
                    raise ValueError('Download exceeded the expected size')
        if size is not None and offset + len(buffer) != size:
            raise ValueError('Download ended before the expected size')
        return send(bytes(buffer), True)

    def _get(self, name: str | None = None, **kwargs) -> requests.Response:
        url = f'https://storage.googleapis.com/storage/v1/b/{quote(self.bucket, safe="")}/o'
        if name is not None:
            url += '/' + quote(name, safe='')
        headers = dict(kwargs.pop('headers', {}))
        for attempt in range(2):
            with self._auth_lock:
                headers['Authorization'] = f'Bearer {self.access_token(refresh=bool(attempt))}'
            response = self.session.get(url, headers=headers, timeout=(15, 120), **kwargs)
            if response.status_code != 401 or attempt:
                return response
            response.close()

    def list_objects(self, prefix: str = '') -> Iterable[dict[str, Any]]:
        """Iterate through every page of bucket objects."""
        params = {'prefix': prefix, 'maxResults': 1000}
        while True:
            with self._get(params=params) as response:
                response.raise_for_status()
                page = response.json()
            yield from page.get('items', [])
            token = page.get('nextPageToken')
            if not token:
                break
            params['pageToken'] = token

    def open_video(self, name: str, byte_range: str | None = None) -> requests.Response:
        """Caller must close the streaming response; preserve HTTP range status."""
        headers = {'Accept-Encoding': 'identity'}
        if byte_range:
            headers['Range'] = byte_range
        return self._get(name, params={'alt': 'media'}, headers=headers, stream=True)

    def close(self) -> None:
        self.session.close()
