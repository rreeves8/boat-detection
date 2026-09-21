"""Layer 2: turn per-clip records into a de-duplicated boat count.

Walks all clips in time order and maintains a list of open "passages" (one real
boat transiting the frame). This is where fragmented boats (exit -> footage
drop -> wake reappears) get merged, leftover wake is discarded, and isolated
wake is inferred to be a missed boat. Timing + direction drive every decision;
see the module-level thresholds to tune for your channel.
"""

from __future__ import annotations

from typing import Any

# A clip that starts within this many seconds of a passage's last activity, in
# the same direction, is treated as that same boat continuing across a dropout.
MERGE_GAP_S = 30
# How long wake keeps churning after a boat has passed. A wake-only clip within
# this window of a real passage is leftover wake, not a new boat.
WAKE_PERSIST_S = 90


def reconcile(
    records: list[dict[str, Any]],
    *,
    merge_gap_s: float = MERGE_GAP_S,
    wake_persist_s: float = WAKE_PERSIST_S,
) -> dict[str, Any]:
    """Reconcile clip records into a boat count.

    Each record needs ``start`` and ``end`` (epoch seconds), ``class``
    (``boat`` | ``wake_only`` | ``empty``) and, for boat clips, a ``boats`` list
    of ``{"direction": ...}``. Returns a summary plus the passage list so every
    count (including inferred/merged ones) can be audited.
    """
    passages: list[dict[str, Any]] = []
    ordered = sorted(records, key=lambda r: r["start"])

    for rec in ordered:
        cls = rec.get("class")
        start, end = rec["start"], rec["end"]

        if cls == "boat":
            for boat in rec.get("boats", []):
                direction = boat.get("direction", "ambiguous")
                match = _recent_passage(
                    passages, start, merge_gap_s, direction=direction
                )
                if match is not None:
                    match["end"] = max(match["end"], end)
                    match["clips"].append(rec.get("name"))
                else:
                    passages.append(_new_passage(rec, direction, end))

        elif cls == "wake_only":
            # Wake trailing any recent passage (direction often unreadable in
            # wake) is leftover; otherwise it implies a boat we never saw.
            match = _recent_passage(passages, start, wake_persist_s)
            if match is not None:
                match["end"] = max(match["end"], end)
                match["clips"].append(rec.get("name"))
                match["trailing_wake"] = True
            else:
                passage = _new_passage(rec, "unknown", end)
                passage["inferred"] = True
                passages.append(passage)
        # empty clips contribute nothing.

    by_direction: dict[str, int] = {}
    inferred = 0
    for p in passages:
        by_direction[p["direction"]] = by_direction.get(p["direction"], 0) + 1
        if p.get("inferred"):
            inferred += 1

    return {
        "total_boats": len(passages),
        "by_direction": by_direction,
        "inferred_from_wake": inferred,
        "clips_processed": len(ordered),
        "passages": passages,
    }


def _new_passage(rec: dict[str, Any], direction: str, end: float) -> dict[str, Any]:
    return {
        "direction": direction,
        "start": rec["start"],
        "end": end,
        "clips": [rec.get("name")],
        "inferred": False,
        "trailing_wake": False,
    }


def _recent_passage(
    passages: list[dict[str, Any]],
    start: float,
    window_s: float,
    *,
    direction: str | None = None,
) -> dict[str, Any] | None:
    """Most recent passage whose last activity is within ``window_s`` of ``start``.

    When ``direction`` is given, a passage only matches if its direction is
    compatible (equal, or either side ambiguous/unknown).
    """
    best = None
    for p in passages:
        gap = start - p["end"]
        if gap < 0 or gap > window_s:
            continue
        if direction is not None and not _direction_compatible(
            p["direction"], direction
        ):
            continue
        if best is None or p["end"] > best["end"]:
            best = p
    return best


def _direction_compatible(a: str, b: str) -> bool:
    soft = {"ambiguous", "unknown"}
    return a == b or a in soft or b in soft
