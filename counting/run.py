"""Orchestrator: pull clips from GCS, analyze each, and store records in GCS.

    python -m counting.run analyze YYYY-MM-DD [...]

Records live only in the ``RECORDS_BUCKET`` object -- there is no local cache.
``analyze`` fetches the existing records to learn which clips are already done
(so it is resumable across runs), appends the new ones, and re-uploads the set.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gcs import GCS  # noqa: E402
from counting.analyze import ClipAnalyzer  # noqa: E402
from counting.daylight import is_daytime  # noqa: E402

# Private bucket the source .mp4 clips are read from.
SOURCE_BUCKET = "traffic-recordings"
# Public bucket the analysis records are published to -- the same place the
# landing page reads them from. The pipeline owns this upload; the Makefile does
# not manage records.
RECORDS_BUCKET = "boats-assets-boat-detection-509220"
# Object name the UI fetches the analysis records from.
RECORDS_OBJECT = "records.jsonl"


def parse_start(name: str) -> float:
    """Epoch seconds from the ``%Y-%m-%d_%H-%M-%S_<event_id>.mp4`` filename (UTC)."""
    stamp = Path(name).name[:19]
    dt = datetime.strptime(stamp, "%Y-%m-%d_%H-%M-%S").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def fetch_records(storage: GCS) -> list[dict]:
    """Load the current records set from the bucket (empty if none exists yet)."""
    with storage.open_video(RECORDS_OBJECT) as response:
        if response.status_code == 404:
            return []
        response.raise_for_status()
        text = response.text
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def upload_records(storage: GCS, records: list[dict]) -> None:
    """Serialize records to JSONL and upload them for the UI to fetch."""
    data = "".join(json.dumps(r) + "\n" for r in records).encode()
    storage.upload_stream(
        RECORDS_OBJECT,
        [data],
        size=len(data),
        content_type="application/x-ndjson",
        overwrite=True,
    )
    print(f"Published {len(records)} records -> gs://{storage.bucket}/{RECORDS_OBJECT} ({len(data)} bytes)")


def download_to(storage: GCS, name: str, dest: Path) -> None:
    with storage.open_video(name) as response:
        response.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)


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
    records_gcs = GCS(RECORDS_BUCKET)
    analyzer = ClipAnalyzer()

    records = fetch_records(records_gcs)
    done = {r["name"] for r in records}
    processed = 0

    for day in args.days:
        for name, start in iter_day_clips(storage, day):
            if name in done:
                continue
            with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
                download_to(storage, name, Path(tmp.name))
                record = analyzer.analyze(tmp.name)
            # Real clip duration from decoded frame count and source fps.
            duration = record["total_frames"] / (record["fps"] or 15.0)
            record.update({"name": name, "start": start, "end": start + duration})
            records.append(record)
            done.add(name)
            processed += 1
            print(f"{name}: {record['moving_count']} moving")

    print(f"\nAnalyzed {processed} new clips (days: {', '.join(args.days)})")
    storage.close()
    if processed and not args.no_publish:
        upload_records(records_gcs, records)
    records_gcs.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="download + analyze clips from GCS")
    p_analyze.add_argument("days", nargs="+", metavar="YYYY-MM-DD",
                           help="one or more days to analyze (daytime clips only)")
    p_analyze.add_argument("--no-publish", action="store_true",
                           help="analyze without uploading the updated records")
    p_analyze.set_defaults(func=cmd_analyze)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
