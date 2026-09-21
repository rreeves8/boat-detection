import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraper_footage import ReolinkCloud
from gcs import GCS


ENV_FILE = Path(__file__).with_name(".env")


def load_env(path: Path = ENV_FILE) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (no override)."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value

def last_completed_file(storage: GCS) -> str | None:
    """Return the most recently uploaded filename, or None if the bucket is empty.

    Filenames are ``%Y-%m-%d_%H-%M-%S_<event_id>.mp4``, whose zero-padded
    timestamp prefix sorts lexicographically in chronological order, so the
    lexicographic maximum is the latest recording already stored.
    """
    latest = None
    for obj in storage.list_objects():
        name = obj["name"]
        if latest is None or name > latest:
            latest = name
    return latest


def main():
    load_env()
    storage = GCS("traffic-recordings")
    last_file = last_completed_file(storage)
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    if last_file:
        start = datetime.strptime(last_file[:19], "%Y-%m-%d_%H-%M-%S").replace(
            tzinfo=timezone.utc
        )
    end = datetime.now(timezone.utc)

    client = ReolinkCloud.from_credentials(
        email=require_env("REOLINK_EMAIL"),
        password=require_env("REOLINK_PASSWORD"),
        totp_secret=require_env("REOLINK_TOTP_SECRET"),
    )

    for video in client.iter_videos(
        start_ms=int(start.timestamp() * 1000),
        end_ms=int(end.timestamp() * 1000),
    ):
        if video["filename"] == last_file:
            continue
        storage.upload_stream(
            video["filename"],
            client.download_video(video),
            size=video.get("size"),
        )


if __name__ == "__main__":
    main()
