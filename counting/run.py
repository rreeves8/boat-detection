"""Orchestrator: pull clips from GCS, analyze each, then reconcile the count.

Two subcommands:

    python -m counting.run analyze   # download + Layer-1 analyze, cache to jsonl
    python -m counting.run count     # Layer-2 reconcile the cached records

``analyze`` is resumable: it skips any clip already present in the records cache,
so it is safe to rerun across thousands of clips or after an interruption.
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
from counting.reconcile import reconcile  # noqa: E402
from counting.daylight import is_daytime  # noqa: E402

BUCKET = "traffic-recordings"
RECORDS_PATH = Path(__file__).with_name("records.jsonl")
# Object name the UI fetches the analysis records from.
RECORDS_OBJECT = "records.jsonl"


def parse_start(name: str) -> float:
    """Epoch seconds from the ``%Y-%m-%d_%H-%M-%S_<event_id>.mp4`` filename (UTC)."""
    stamp = Path(name).name[:19]
    dt = datetime.strptime(stamp, "%Y-%m-%d_%H-%M-%S").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def load_done(path: Path = RECORDS_PATH) -> set[str]:
    if not path.exists():
        return set()
    done = set()
    for line in path.read_text().splitlines():
        if line.strip():
            done.add(json.loads(line)["name"])
    return done


def load_records(path: Path = RECORDS_PATH) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def publish_records(storage: GCS, path: Path = RECORDS_PATH) -> None:
    """Upload the local records file to the bucket for the UI to fetch."""
    if not path.exists():
        return
    data = path.read_bytes()
    storage.upload_stream(
        RECORDS_OBJECT,
        [data],
        size=len(data),
        content_type="application/x-ndjson",
        overwrite=True,
    )
    print(f"Published {path.name} -> gs://{BUCKET}/{RECORDS_OBJECT} ({len(data)} bytes)")


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
    storage = GCS(BUCKET)
    analyzer = ClipAnalyzer()
    done = load_done()
    processed = 0

    with RECORDS_PATH.open("a") as out:
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
                out.write(json.dumps(record) + "\n")
                out.flush()
                done.add(name)
                processed += 1
                dirs = ", ".join(o["direction"] for o in record["objects"]) or "-"
                print(f"{name}: {record['moving_count']} moving [{dirs}]")

    print(f"\nAnalyzed {processed} new clips (days: {', '.join(args.days)}) -> {RECORDS_PATH}")
    if not args.no_publish:
        publish_records(storage)
    storage.close()


def cmd_count(args: argparse.Namespace) -> None:
    records = load_records()
    if not records:
        raise SystemExit("No records found; run 'analyze' first.")
    summary = reconcile(
        records, merge_gap_s=args.merge_gap, wake_persist_s=args.wake_persist
    )
    passages = summary.pop("passages")
    print(json.dumps(summary, indent=2))
    if args.verbose:
        print("\nPassages:")
        for p in passages:
            flags = []
            if p["inferred"]:
                flags.append("inferred")
            if p["trailing_wake"]:
                flags.append("+wake")
            when = datetime.fromtimestamp(p["start"], timezone.utc).isoformat()
            print(f"  {when}  {p['direction']:<10} {' '.join(flags)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="download + analyze clips from GCS")
    p_analyze.add_argument("days", nargs="+", metavar="YYYY-MM-DD",
                           help="one or more days to analyze (daytime clips only)")
    p_analyze.add_argument("--no-publish", action="store_true",
                           help="skip uploading records.jsonl to the bucket")
    p_analyze.set_defaults(func=cmd_analyze)

    p_count = sub.add_parser("count", help="reconcile cached records into a count")
    p_count.add_argument("--merge-gap", type=float, default=30.0)
    p_count.add_argument("--wake-persist", type=float, default=90.0)
    p_count.add_argument("-v", "--verbose", action="store_true")
    p_count.set_defaults(func=cmd_count)

    p_publish = sub.add_parser("publish", help="upload records.jsonl to the bucket")
    p_publish.set_defaults(func=lambda a: publish_records(GCS(BUCKET)))

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
