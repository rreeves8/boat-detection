"""Supabase-backed record store -- the pipeline's only persistence.

Public read / private edit: writes here use the **secret** key (server-side only).
The UI reads client-side with the publishable key. Config comes from the env
(SUPABASE_URL, SUPABASE_SECRET_KEY); locally, `source .env` first.
"""

from __future__ import annotations

import os
from typing import Any

import requests

TABLE = "clips"
_TIMEOUT = 30


def _base() -> str:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("SUPABASE_URL is not set (source .env)")
    return f"{url}/rest/v1/{TABLE}"


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    key = os.environ.get("SUPABASE_SECRET_KEY", "")
    if not key:
        raise RuntimeError("SUPABASE_SECRET_KEY is not set (source .env)")
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    if extra:
        headers.update(extra)
    return headers


def existing_names() -> set[str]:
    """Every clip name already stored -- used to resume without re-analyzing."""
    names: set[str] = set()
    offset, page = 0, 1000
    while True:
        resp = requests.get(
            _base(),
            headers=_headers({"Range": f"{offset}-{offset + page - 1}"}),
            params={"select": "name"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json()
        names.update(row["name"] for row in batch)
        if len(batch) < page:
            return names
        offset += page


def upsert(row: dict[str, Any]) -> None:
    """Insert or overwrite one clip row (idempotent -- re-analyzing updates it)."""
    resp = requests.post(
        _base(),
        headers=_headers({
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }),
        json=row,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
