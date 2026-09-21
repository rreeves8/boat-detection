"""Layer 1: find things moving against the static scene -- OpenCV MOG2.

Class-agnostic moving-object detection for a fixed camera, using OpenCV's
built-in adaptive background model (``BackgroundSubtractorMOG2``):

  1. Pass 1 learns the still background of the clip (water, dock, shoreline).
  2. Pass 2 asks the model "what doesn't belong" in each frame -> a motion mask;
     contours on it give a box around every moving blob and its position.
  3. Nearby boxes are merged (a boat and its wake become one object), then a
     light centroid tracker links boxes across frames so each mover is counted
     once and its box path is recorded.

It reports *what moved and where*, never *what the object is* -- so it ignores
the static dock chairs and shoreline houses an object detector mistakes for
boats. Run it on one clip to watch it work::

    python -m counting.analyze path/to/clip.mp4 --out annotated.mp4
"""

from __future__ import annotations

import argparse
from math import hypot
from typing import Any

import cv2

# Frames are downscaled to this width before analysis; all areas/positions are
# fractions, so results are resolution-independent.
PROC_WIDTH = 480
# MOG2 background model: how many frames of history to model, and the
# variance threshold for calling a pixel "foreground". Higher threshold = only
# strong changes (a boat/wake) register, low-amplitude ripple is absorbed.
MOG2_HISTORY = 200
MOG2_VAR_THRESHOLD = 40
# A blob at least this fraction of the frame is a moving object.
MIN_BLOB_AREA_FRAC = 0.0008
# A blob must be this "solid" (filled area / bounding box) to count -- a compact
# mover survives, diffuse ripple speckle is rejected.
MIN_BLOB_SOLIDITY = 0.30
# Detections within this fraction of the frame width of each other are merged
# into one box, so a boat and its churning wake count as a single object.
MERGE_GAP_FRAC = 0.04
# Max centroid jump between frames to link a blob to an existing track, as a
# fraction of the frame diagonal.
MAX_MATCH_DIST = 0.25
# Frames a track may go unmatched before it is closed (rides through occlusion
# behind the dock post, momentary dropout, etc.).
MAX_MISSING_FRAMES = 15
# A track must be seen in at least this many frames to count -- filters the
# one- or two-frame glint that flickers on otherwise-empty water.
MIN_TRACK_FRAMES = 6
# Total travel span (fraction of frame diagonal) required to count as moving
# across the frame rather than jittering in place.
MOVE_MIN_SPAN = 0.05


class ClipAnalyzer:
    """Reusable analyzer that processes many clips; holds no per-clip state."""

    def __init__(
        self,
        *,
        var_threshold: int = MOG2_VAR_THRESHOLD,
        min_blob_area_frac: float = MIN_BLOB_AREA_FRAC,
    ):
        self.var_threshold = var_threshold
        self.min_blob_area_frac = min_blob_area_frac
        # Opening removes isolated speckle; dilation grows a mover and its wake
        # into one solid blob instead of a scatter of fragments.
        self._open_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self._dilate_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))

    def analyze(
        self,
        path: str,
        *,
        meta: dict[str, Any] | None = None,
        annotate_path: str | None = None,
    ) -> dict[str, Any]:
        """Return a moving-object record for one video file.

        If ``annotate_path`` is given, also write a copy of the video with a box
        drawn around every moving object and a live count in the corner.
        """
        frames, size, fps, total_frames = self._read_frames(path)
        if len(frames) < 2:
            record = _empty_record(size, fps, total_frames)
            if meta:
                record.update(meta)
            return record

        proc_h, proc_w = frames[0].shape[:2]
        diagonal = hypot(proc_w, proc_h)
        min_area = self.min_blob_area_frac * (proc_w * proc_h)

        # Pass 1: learn the static background from the whole clip.
        bg = cv2.createBackgroundSubtractorMOG2(
            history=MOG2_HISTORY, varThreshold=self.var_threshold, detectShadows=False
        )
        for frame in frames:
            bg.apply(frame)

        # Pass 2: detect movers against the learned background.
        writer = None
        if annotate_path:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(annotate_path, fourcc, fps, (proc_w, proc_h))
        tracker = _CentroidTracker(diagonal)
        for frame in frames:
            detections = self._detect_boxes(bg, frame, min_area, proc_w)
            tracker.update(detections)
            if writer is not None:
                writer.write(_annotate(frame.copy(), detections))
        if writer is not None:
            writer.release()

        objects = _moving_objects(tracker.finish(), proc_w, proc_h, diagonal)
        record: dict[str, Any] = {
            "moving_object": bool(objects),
            "moving_count": len(objects),
            "objects": objects,
            "total_frames": total_frames,
            "fps": round(fps, 2),
            "resolution": [int(size[0]), int(size[1])],
        }
        if meta:
            record.update(meta)
        return record

    def _detect_boxes(self, bg, frame, min_area, proc_w) -> list[dict[str, float]]:
        """Boxes around whatever the background model flags as moving."""
        mask = bg.apply(frame, learningRate=0)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._open_kernel)
        mask = cv2.dilate(mask, self._dilate_kernel, iterations=2)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        rects = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < min_area:
                continue
            x, y, w, h = cv2.boundingRect(c)
            if w * h == 0 or area / (w * h) < MIN_BLOB_SOLIDITY:
                continue
            rects.append((x, y, w, h))
        rects = _merge_rects(rects, MERGE_GAP_FRAC * proc_w)
        return [
            {
                "x": x, "y": y, "w": w, "h": h,
                "cx": x + w / 2.0, "cy": y + h / 2.0, "area": float(w * h),
            }
            for (x, y, w, h) in rects
        ]

    def _read_frames(self, path: str):
        """Decode the clip, downscaled to PROC_WIDTH (colour, for MOG2)."""
        cap = cv2.VideoCapture(path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or PROC_WIDTH
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or PROC_WIDTH
        scale = PROC_WIDTH / src_w if src_w else 1.0
        proc_size = (PROC_WIDTH, max(1, round(src_h * scale)))
        frames, total = [], 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            total += 1
            frames.append(cv2.resize(frame, proc_size, interpolation=cv2.INTER_AREA))
        cap.release()
        return frames, (src_w, src_h), fps, total


class _CentroidTracker:
    """Greedy nearest-centroid multi-object tracker over motion boxes."""

    def __init__(self, diagonal: float):
        self.diagonal = diagonal
        self.max_dist = MAX_MATCH_DIST * diagonal
        self.active: list[dict[str, Any]] = []
        self.done: list[dict[str, Any]] = []
        self.frame = -1

    def update(self, detections: list[dict[str, float]]) -> None:
        self.frame += 1
        # Rank every viable (track, detection) pair by distance, assign greedily.
        pairs = []
        for ti, track in enumerate(self.active):
            for di, det in enumerate(detections):
                dist = hypot(track["cx"] - det["cx"], track["cy"] - det["cy"])
                if dist <= self.max_dist:
                    pairs.append((dist, ti, di))
        pairs.sort(key=lambda p: p[0])

        matched_t: set[int] = set()
        matched_d: set[int] = set()
        for _, ti, di in pairs:
            if ti in matched_t or di in matched_d:
                continue
            matched_t.add(ti)
            matched_d.add(di)
            self._extend(self.active[ti], detections[di])

        for ti, track in enumerate(self.active):
            if ti not in matched_t:
                track["missing"] += 1
        for di, det in enumerate(detections):
            if di not in matched_d:
                self.active.append(_new_track(det, self.frame))

        survivors = []
        for track in self.active:
            if track["missing"] > MAX_MISSING_FRAMES:
                self.done.append(track)
            else:
                survivors.append(track)
        self.active = survivors

    def _extend(self, track: dict[str, Any], det: dict[str, float]) -> None:
        track["cx"], track["cy"] = det["cx"], det["cy"]
        track["x_min"] = min(track["x_min"], det["cx"])
        track["x_max"] = max(track["x_max"], det["cx"])
        track["y_min"] = min(track["y_min"], det["cy"])
        track["y_max"] = max(track["y_max"], det["cy"])
        track["max_area"] = max(track["max_area"], det["area"])
        if det["area"] >= track["max_area"]:
            track["box"] = (det["x"], det["y"], det["w"], det["h"])
        # Per-frame trajectory, so the UI can replay the box moving with the object.
        track["path"].append([self.frame, det["x"], det["y"], det["w"], det["h"]])
        track["frames"] += 1
        track["missing"] = 0

    def finish(self) -> list[dict[str, Any]]:
        return self.done + self.active


def _merge_rects(rects: list[tuple], gap: float) -> list[tuple]:
    """Union boxes that overlap or sit within ``gap`` pixels of each other."""
    boxes = list(rects)
    merged = True
    while merged:
        merged = False
        out: list[list] = []
        for box in boxes:
            for other in out:
                if _near(box, other, gap):
                    ox, oy = min(other[0], box[0]), min(other[1], box[1])
                    ox2 = max(other[0] + other[2], box[0] + box[2])
                    oy2 = max(other[1] + other[3], box[1] + box[3])
                    other[0], other[1], other[2], other[3] = ox, oy, ox2 - ox, oy2 - oy
                    merged = True
                    break
            else:
                out.append(list(box))
        boxes = [tuple(b) for b in out]
    return boxes


def _near(a: tuple, b: tuple, gap: float) -> bool:
    """True if boxes overlap or are within ``gap`` pixels on both axes."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return (ax <= bx + bw + gap and bx <= ax + aw + gap
            and ay <= by + bh + gap and by <= ay + ah + gap)


def _new_track(det: dict[str, float], frame: int) -> dict[str, Any]:
    return {
        "cx": det["cx"], "cy": det["cy"],
        "x_min": det["cx"], "x_max": det["cx"],
        "y_min": det["cy"], "y_max": det["cy"],
        "max_area": det["area"],
        "box": (det["x"], det["y"], det["w"], det["h"]),
        "path": [[frame, det["x"], det["y"], det["w"], det["h"]]],
        "frames": 1,
        "missing": 0,
    }


def _moving_objects(tracks, proc_w, proc_h, diagonal) -> list[dict[str, Any]]:
    """Keep tracks that persisted and travelled; report box + box path."""
    objects = []
    for track in tracks:
        if track["frames"] < MIN_TRACK_FRAMES:
            continue
        span = hypot(track["x_max"] - track["x_min"], track["y_max"] - track["y_min"])
        span_frac = span / diagonal
        if span_frac < MOVE_MIN_SPAN:
            continue
        x, y, w, h = track["box"]
        # Trajectory as [frame, x, y, w, h] with coords normalized 0-1, so the UI
        # can scale each box to the playing video and move it frame by frame.
        path = [
            [f, round(bx / proc_w, 4), round(by / proc_h, 4),
             round(bw / proc_w, 4), round(bh / proc_h, 4)]
            for (f, bx, by, bw, bh) in track["path"]
        ]
        objects.append(
            {
                "frames_visible": track["frames"],
                "travel": round(span_frac, 3),
                # Box in processing-resolution pixels [x, y, w, h].
                "box": [round(x), round(y), round(w), round(h)],
                "track": path,
            }
        )
    return objects


def _annotate(frame, detections):
    """Draw motion boxes and a live count onto a frame."""
    for det in detections:
        x, y, w, h = int(det["x"]), int(det["y"]), int(det["w"]), int(det["h"])
        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
    cv2.putText(frame, f"moving: {len(detections)}", (12, 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2, cv2.LINE_AA)
    return frame


def _empty_record(size, fps, total_frames) -> dict[str, Any]:
    return {
        "moving_object": False,
        "moving_count": 0,
        "objects": [],
        "total_frames": total_frames,
        "fps": round(fps, 2),
        "resolution": [int(size[0]), int(size[1])],
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description="MOG2 moving-object detection on one clip.")
    parser.add_argument("video", help="path to a video file")
    parser.add_argument("--out", help="write an annotated copy with boxes drawn")
    args = parser.parse_args()

    record = ClipAnalyzer().analyze(args.video, annotate_path=args.out)
    print(f"moving objects: {record['moving_count']}")
    for i, obj in enumerate(record["objects"], 1):
        print(f"  #{i} box={obj['box']} "
              f"frames={obj['frames_visible']} travel={obj['travel']}")
    if args.out:
        print(f"annotated video -> {args.out}")


if __name__ == "__main__":
    _main()
