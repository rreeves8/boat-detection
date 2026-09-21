"""Layer 1 (AI variant): detect boats + people with an open-vocabulary model.

Drop-in alternative to :mod:`counting.analyze`. Instead of frame differencing it
runs YOLO-World (prompted with the class names we care about) on sampled frames,
then reuses the same centroid tracker + "must actually travel" filter as the
motion detector. That travel filter is what makes zero-shot detection usable on
this fixed camera: the far-shore houses YOLO-World mislabels as "boat" never
move, so they fail the travel test and drop out, while real boats survive.

Produces the exact same record schema as :mod:`counting.analyze`, so
``reconcile.py``, ``run.py`` and the UI all work unchanged::

    python -m counting.detect_yolo path/to/clip.mp4 --out annotated.mp4
"""

from __future__ import annotations

import argparse
from math import hypot
from typing import Any

import cv2

from counting.analyze import (
    MAX_MISSING_FRAMES,
    MIN_TRACK_FRAMES,
    MOVE_MIN_SPAN,
    DIRECTION_MIN_TRAVEL,
    MAX_MATCH_DIST,
)

# Classes we prompt the open-vocabulary detector with.
CLASSES = ["boat", "person"]
# Frames are resized to this width for detection; big enough to resolve the
# small distant boats (they vanish at 640).
IMG_SIZE = 1280
# Minimum detector confidence to accept a box. Real boats sit ~0.4-0.58 here;
# the static shoreline false "boats" sit ~0.25-0.30, and the travel filter mops
# up whatever still leaks through.
CONF_THRESHOLD = 0.30
# We don't need every frame to count an object -- sample ~this many per second.
SAMPLE_FPS = 2.0

_MODEL = None


def _model():
    """Load YOLO-World once (on the GPU/MPS if available)."""
    global _MODEL
    if _MODEL is None:
        from ultralytics import YOLOWorld

        _MODEL = YOLOWorld("yolov8s-world.pt")
        _MODEL.set_classes(CLASSES)
    return _MODEL


class YoloClipAnalyzer:
    """Same interface as counting.analyze.ClipAnalyzer, backed by YOLO-World."""

    def __init__(self, *, conf: float = CONF_THRESHOLD, sample_fps: float = SAMPLE_FPS):
        self.conf = conf
        self.sample_fps = sample_fps

    def analyze(
        self,
        path: str,
        *,
        meta: dict[str, Any] | None = None,
        annotate_path: str | None = None,
    ) -> dict[str, Any]:
        cap = cv2.VideoCapture(path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or IMG_SIZE
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or IMG_SIZE
        stride = max(1, round(fps / self.sample_fps))
        diagonal = hypot(src_w, src_h)

        writer = None
        if annotate_path:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(annotate_path, fourcc, fps, (src_w, src_h))

        model = _model()
        tracker = _LabelTracker(diagonal)
        total_frames = 0
        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            total_frames += 1
            if idx % stride == 0:
                dets = self._detect(model, frame)
                tracker.update(dets)
                if writer is not None:
                    _draw(frame, dets)
            if writer is not None:
                writer.write(frame)
            idx += 1
        cap.release()
        if writer is not None:
            writer.release()

        objects = _moving_objects(tracker.finish(), src_w, diagonal)
        boats = [o for o in objects if o["type"] == "boat"]
        cls = "boat" if boats else ("person" if objects else "empty")
        record: dict[str, Any] = {
            "moving_object": bool(objects),
            "moving_count": len(objects),
            "objects": objects,
            # Compatibility fields for reconcile.py / the UI. "boats" drives the
            # UI's pass count, so keep it to boat-class objects.
            "class": cls,
            "boats": boats,
            "people_count": len(objects) - len(boats),
            "detector": "yolo-world",
            "total_frames": total_frames,
            "fps": round(fps, 2),
            "resolution": [int(src_w), int(src_h)],
        }
        if meta:
            record.update(meta)
        return record

    def _detect(self, model, frame) -> list[dict[str, Any]]:
        result = model.predict(frame, imgsz=IMG_SIZE, conf=self.conf, verbose=False)[0]
        dets = []
        for box, cls, conf in zip(
            result.boxes.xyxy.cpu().numpy(),
            result.boxes.cls.cpu().numpy(),
            result.boxes.conf.cpu().numpy(),
        ):
            x1, y1, x2, y2 = box
            dets.append(
                {
                    "label": model.names[int(cls)],
                    "conf": float(conf),
                    "x": float(x1), "y": float(y1),
                    "w": float(x2 - x1), "h": float(y2 - y1),
                    "cx": float((x1 + x2) / 2), "cy": float((y1 + y2) / 2),
                    "area": float((x2 - x1) * (y2 - y1)),
                }
            )
        return dets


class _LabelTracker:
    """Greedy nearest-centroid tracker that also carries each track's label."""

    def __init__(self, diagonal: float):
        self.max_dist = MAX_MATCH_DIST * diagonal
        self.active: list[dict[str, Any]] = []
        self.done: list[dict[str, Any]] = []

    def update(self, detections: list[dict[str, Any]]) -> None:
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
                self.active.append(_new_track(det))

        survivors = []
        for track in self.active:
            # Sampled frames are sparse, so tolerate more gaps between hits.
            if track["missing"] > MAX_MISSING_FRAMES:
                self.done.append(track)
            else:
                survivors.append(track)
        self.active = survivors

    def _extend(self, track: dict[str, Any], det: dict[str, Any]) -> None:
        track["cx"], track["cy"] = det["cx"], det["cy"]
        track["last_x"] = det["cx"]
        track["x_min"] = min(track["x_min"], det["cx"])
        track["x_max"] = max(track["x_max"], det["cx"])
        track["y_min"] = min(track["y_min"], det["cy"])
        track["y_max"] = max(track["y_max"], det["cy"])
        track["votes"][det["label"]] = track["votes"].get(det["label"], 0) + 1
        if det["conf"] >= track["best_conf"]:
            track["best_conf"] = det["conf"]
            track["box"] = (det["x"], det["y"], det["w"], det["h"])
        track["frames"] += 1
        track["missing"] = 0

    def finish(self) -> list[dict[str, Any]]:
        return self.done + self.active


def _new_track(det: dict[str, Any]) -> dict[str, Any]:
    return {
        "cx": det["cx"], "cy": det["cy"],
        "first_x": det["cx"], "last_x": det["cx"],
        "x_min": det["cx"], "x_max": det["cx"],
        "y_min": det["cy"], "y_max": det["cy"],
        "votes": {det["label"]: 1},
        "best_conf": det["conf"],
        "box": (det["x"], det["y"], det["w"], det["h"]),
        "frames": 1,
        "missing": 0,
    }


def _moving_objects(tracks, src_w, diagonal) -> list[dict[str, Any]]:
    objects = []
    for track in tracks:
        if track["frames"] < MIN_TRACK_FRAMES:
            continue
        span = hypot(track["x_max"] - track["x_min"], track["y_max"] - track["y_min"])
        span_frac = span / diagonal
        if span_frac < MOVE_MIN_SPAN:
            continue  # static shoreline house / dock -- never travels.
        net_dx = (track["last_x"] - track["first_x"]) / max(src_w, 1)
        if net_dx > DIRECTION_MIN_TRAVEL:
            direction = "L->R"
        elif net_dx < -DIRECTION_MIN_TRAVEL:
            direction = "R->L"
        else:
            direction = "ambiguous"
        label = max(track["votes"], key=track["votes"].get)
        x, y, w, h = track["box"]
        objects.append(
            {
                "type": label,
                "direction": direction,
                "frames_visible": track["frames"],
                "travel": round(span_frac, 3),
                "confidence": round(track["best_conf"], 2),
                "box": [round(x), round(y), round(w), round(h)],
            }
        )
    return objects


def _draw(frame, dets) -> None:
    for d in dets:
        x, y, w, h = int(d["x"]), int(d["y"]), int(d["w"]), int(d["h"])
        colour = (0, 255, 0) if d["label"] == "boat" else (0, 200, 255)
        cv2.rectangle(frame, (x, y), (x + w, y + h), colour, 2)
        cv2.putText(frame, f'{d["label"]} {d["conf"]:.2f}', (x, max(14, y - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1, cv2.LINE_AA)


def _main() -> None:
    parser = argparse.ArgumentParser(description="YOLO-World boat/person detection on one clip.")
    parser.add_argument("video")
    parser.add_argument("--out")
    args = parser.parse_args()
    record = YoloClipAnalyzer().analyze(args.video, annotate_path=args.out)
    print(f"moving objects: {record['moving_count']} "
          f"(boats={len(record['boats'])}, people={record['people_count']})")
    for i, o in enumerate(record["objects"], 1):
        print(f"  #{i} {o['type']:<6} {o['direction']:<10} conf={o['confidence']} "
              f"box={o['box']} travel={o['travel']}")
    if args.out:
        print(f"annotated -> {args.out}")


if __name__ == "__main__":
    _main()
