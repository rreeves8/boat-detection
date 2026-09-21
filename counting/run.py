"""Orchestrator: pull clips from GCS, analyze each, and store records in Supabase.

    python -m counting.run analyze YYYY-MM-DD [...]          # skip clips already stored
    python -m counting.run analyze YYYY-MM-DD [...] --force   # re-analyze + overwrite

Each clip is upserted as one row (counting.store) as soon as it's analyzed, so
runs are resumable and crash-safe, and re-analyzing a clip just updates its row.
Requires SUPABASE_URL + SUPABASE_SECRET_KEY in the env (source .env locally).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gcs import GCS  # noqa: E402
from counting.analyze import ClipAnalyzer  # noqa: E402
from counting.daylight import is_daytime  # noqa: E402
from counting import store  # noqa: E402

# Private bucket the source .mp4 clips are read from.
SOURCE_BUCKET = "traffic-recordings"


def parse_start(name: str) -> float:
    """Epoch seconds from the ``%Y-%m-%d_%H-%M-%S_<event_id>.mp4`` filename (UTC)."""
    stamp = Path(name).name[:19]
    dt = datetime.strptime(stamp, "%Y-%m-%d_%H-%M-%S").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def to_row(name: str, start: float, record: dict) -> dict:
    """Map an analyzer record to a ``clips`` table row."""
    return {
        "name": name,
        "recorded_at": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "moving_count": record["moving_count"],
        "total_frames": record["total_frames"],
        "fps": record["fps"],
        "resolution": record["resolution"],
        "objects": record["objects"],
    }


def download_to(storage: GCS, name: str, dest: Path) -> None:
    with storage.open_video(name) as response:
        response.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)


def all_days(storage: GCS) -> list[str]:
    """Distinct YYYY-MM-DD dates that have .mp4 clips in the bucket."""
    days = set()
    for obj in storage.list_objects():
        name = obj["name"]
        if name.endswith(".mp4") and obj.get("size"):
            days.add(name[:10])
    return sorted(days)


def iter_day_clips(storage: GCS, day: str):
    """Yield ``(name, start_epoch)`` for daytime .mp4 clips recorded on ``day``."""
    for obj in storage.list_objects(prefix=f"{day}_"):
        name = obj["name"]
        if not name.endswith(".mp4") or not obj.get("size"):
            continue
        try:
            start = parse_start(name)
        except ValueError:
            continue
        if is_daytime(start, day):
            yield name, start


def cmd_analyze(args: argparse.Namespace) -> None:
    storage = GCS(SOURCE_BUCKET)
    analyzer = ClipAnalyzer()

    done = set() if args.force else store.existing_names()
    days = args.days or all_days(storage)
    processed = 0

    for day in days:
        for name, start in iter_day_clips(storage, day):
            if name in done:
                continue
            with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
                download_to(storage, name, Path(tmp.name))
                record = analyzer.analyze(tmp.name)
            store.upsert(to_row(name, start, record))
            processed += 1
            print(f"{name}: {record['moving_count']} moving")

    print(f"\nAnalyzed {processed} clips across {len(days)} day(s) -> Supabase")
    storage.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="download + analyze clips from GCS")
    p_analyze.add_argument("days", nargs="*", metavar="YYYY-MM-DD",
                           help="days to analyze (daytime clips only); omit to do every day in the bucket")
    p_analyze.add_argument("--force", action="store_true",
                           help="re-analyze and overwrite clips already stored")
    p_analyze.set_defaults(func=cmd_analyze)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
