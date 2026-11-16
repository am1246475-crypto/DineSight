"""
cv/counting.py

Real-time Entry/Exit Counting using Polygon Zones.

Input:
[
    {
        "track_id": 5,
        "bbox": [x1, y1, x2, y2],
        "conf": 0.9
    }
]

Output:
[
    {
        "camera_id": "entrance",
        "track_id": 5,
        "event_type": "enter",
        "timestamp": "...",
        "meta": {...}
    }
]

Zones:
    outside -> door -> inside = enter
    inside -> door -> outside = exit

All polygon coordinates are normalized (0..1).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np


Point = Tuple[float, float]
Polygon = List[Point]

# --------------------------------------------------
# Default polygons: REPLACE WITH REAL CAMERA COORDS
# --------------------------------------------------

DEFAULT_OUTSIDE: Polygon = [
    (0.0, 0.0),
    (0.3, 0.0),
    (0.3, 1.0),
    (0.0, 1.0),
]

DEFAULT_DOOR: Polygon = [
    (0.3, 0.0),
    (0.7, 0.0),
    (0.7, 1.0),
    (0.3, 1.0),
]

DEFAULT_INSIDE: Polygon = [
    (0.7, 0.0),
    (1.0, 0.0),
    (1.0, 1.0),
    (0.7, 1.0),
]


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )


def parse_polygon(text: str) -> Polygon:
    """
    Format:
    x1,y1;x2,y2;x3,y3;...

    Coordinates must be normalized between 0 and 1.
    """

    try:
        points = []

        for item in text.split(";"):
            x, y = map(float, item.split(","))
            if not (0 <= x <= 1 and 0 <= y <= 1):
                raise ValueError

            points.append((x, y))

        if len(points) < 3:
            raise ValueError

        return points

    except ValueError:
        raise argparse.ArgumentTypeError(
            "Polygon format: x1,y1;x2,y2;x3,y3 (coordinates 0..1)"
        )


class ZoneCounter:

    def __init__(
        self,
        camera_id: str = "entrance",
        outside: Polygon = DEFAULT_OUTSIDE,
        door: Polygon = DEFAULT_DOOR,
        inside: Polygon = DEFAULT_INSIDE,
        anchor: str = "bottom",
        stable_frames: int = 3,
        door_timeout: int = 90,
        state_ttl_frames: int = 300,
    ) -> None:

        if anchor not in ("bottom", "center"):
            raise ValueError("anchor must be bottom or center")

        self.camera_id = camera_id

        self.polygons = {
            "outside": outside,
            "door": door,
            "inside": inside,
        }

        self.anchor = anchor
        self.stable_frames = stable_frames
        self.door_timeout = door_timeout
        self.state_ttl_frames = state_ttl_frames

        self.entered = 0
        self.exited = 0

        self._state: Dict[int, Dict[str, Any]] = {}
        self._frame_no = 0

    # --------------------------------------------------
    # Properties
    # --------------------------------------------------

    @property
    def current(self) -> int:
        return max(0, self.entered - self.exited)

    # --------------------------------------------------
    # Geometry
    # --------------------------------------------------

    def _anchor_point(self, bbox: Sequence[float]) -> Point:

        x1, y1, x2, y2 = bbox

        cx = (x1 + x2) / 2.0

        if self.anchor == "bottom":
            return cx, y2

        return cx, (y1 + y2) / 2.0

    def _get_zone(
        self,
        point: Point,
        width: int,
        height: int,
    ) -> Optional[str]:

        x, y = point

        # Door has priority in case of polygon overlap.
        priority = ("door", "inside", "outside")

        for zone in priority:

            polygon = np.array(
                [
                    (int(px * width), int(py * height))
                    for px, py in self.polygons[zone]
                ],
                dtype=np.int32,
            )

            result = cv2.pointPolygonTest(
                polygon,
                (float(x), float(y)),
                False,
            )

            if result >= 0:
                return zone

        return None

    # --------------------------------------------------
    # State management
    # --------------------------------------------------

    def _new_state(self) -> Dict[str, Any]:

        return {
            "stable_zone": None,
            "candidate_zone": None,
            "candidate_frames": 0,
            "origin_zone": None,
            "door_started_frame": None,
            "last_seen": self._frame_no,
        }

    def _stabilize_zone(
        self,
        state: Dict[str, Any],
        detected_zone: Optional[str],
    ) -> Optional[Tuple[Optional[str], str]]:
        """Returns (previous_zone, new_zone) once a zone change has been
        seen for `stable_frames` frames in a row, otherwise None."""

        if detected_zone is None:
            state["candidate_zone"] = None
            state["candidate_frames"] = 0
            return None

        if detected_zone == state["stable_zone"]:
            state["candidate_zone"] = None
            state["candidate_frames"] = 0
            return None

        if detected_zone != state["candidate_zone"]:

            state["candidate_zone"] = detected_zone
            state["candidate_frames"] = 1

        else:
            state["candidate_frames"] += 1

        # FIX: this block was indented inside the `else:` above, so it was
        # skipped on the first frame (stable_frames=1 needed 2 frames).
        if state["candidate_frames"] >= self.stable_frames:
            previous = state["stable_zone"]

            state["stable_zone"] = detected_zone
            state["candidate_zone"] = None
            state["candidate_frames"] = 0

            return previous, detected_zone

        return None

    # --------------------------------------------------
    # Transition validation
    # --------------------------------------------------

    def _handle_transition(
        self,
        track_id,
        state,
        old_zone,
        new_zone,
    ):
        origin = state["origin_zone"]

        if new_zone == "door":
            if old_zone in ("outside", "inside"):
                state["origin_zone"] = old_zone
                state["door_started_frame"] = self._frame_no
            return None

        if old_zone == "door":
            state["origin_zone"] = None
            state["door_started_frame"] = None

            if origin == "outside" and new_zone == "inside":
                return "enter"

            if origin == "inside" and new_zone == "outside":
                return "exit"

        return None

    # --------------------------------------------------
    # Main API
    # --------------------------------------------------

    def update(
        self,
        tracks: Sequence[Dict[str, Any]],
        frame_shape: Sequence[int],
        frame_idx: Optional[int] = None,
    ) -> List[Dict[str, Any]]:

        height = int(frame_shape[0])
        width = int(frame_shape[1])

        self._frame_no = (
            frame_idx
            if frame_idx is not None
            else self._frame_no + 1
        )

        events = []

        for trk in tracks:

            if "track_id" not in trk or "bbox" not in trk:
                continue

            tid = int(trk["track_id"])

            point = self._anchor_point(trk["bbox"])

            detected_zone = self._get_zone(
                point,
                width,
                height,
            )

            state = self._state.setdefault(
                tid,
                self._new_state(),
            )

            state["last_seen"] = self._frame_no

            # Cancel incomplete crossings after timeout.
            started = state["door_started_frame"]

            if (
                started is not None
                and self._frame_no - started > self.door_timeout
            ):
                state["origin_zone"] = None
                state["door_started_frame"] = None

            transition = self._stabilize_zone(
                state,
                detected_zone,
            )

            if transition is None:
                continue

            # FIX: _stabilize_zone returns (old_zone, new_zone) and
            # _handle_transition needs both. The old call passed only
            # 3 arguments, which raised TypeError on the first zone change.
            old_zone, new_zone = transition

            event_type = self._handle_transition(
                tid,
                state,
                old_zone,
                new_zone,
            )

            if event_type is None:
                continue

            if event_type == "enter":
                self.entered += 1

            elif event_type == "exit":
                self.exited += 1

            events.append(
                {
                    "camera_id": self.camera_id,
                    "track_id": tid,
                    "event_type": event_type,
                    "timestamp": utc_now_iso(),
                    "meta": {
                        "frame": self._frame_no,
                        "anchor": [
                            round(point[0], 1),
                            round(point[1], 1),
                        ],
                        "transition": (
                            "outside->door->inside"
                            if event_type == "enter"
                            else "inside->door->outside"
                        ),
                    },
                }
            )

        # Remove expired tracks.
        expired = [
            tid
            for tid, state in self._state.items()
            if self._frame_no - state["last_seen"]
            > self.state_ttl_frames
        ]

        for tid in expired:
            del self._state[tid]

        return events


# --------------------------------------------------
# Replay
# --------------------------------------------------

def _replay(path: str, args: argparse.Namespace) -> None:

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    meta = {}
    frames = raw

    if isinstance(raw, dict):
        meta = raw.get("meta", {}) or {}
        frames = raw.get("frames", [])

    width = args.width or meta.get("width")
    height = args.height or meta.get("height")

    if not width or not height:
        raise SystemExit(
            "Missing video dimensions. Use --width and --height."
        )

    counter = ZoneCounter(
        camera_id=args.camera_id,
        outside=args.outside,
        door=args.door,
        inside=args.inside,
        anchor=args.anchor,
        stable_frames=args.stable_frames,
        door_timeout=args.door_timeout,
    )

    for i, fr in enumerate(frames):

        if isinstance(fr, list):
            tracks, idx = fr, i
        else:
            tracks = fr.get(
                "detections",
                fr.get("tracks", []),
            )
            idx = fr.get("frame_id", fr.get("frame", i))

        events = counter.update(
            tracks,
            (int(height), int(width)),
            frame_idx=idx,
        )

        for event in events:
            print(json.dumps(event))

    print(
        f"frames={len(frames)} "
        f"entered={counter.entered} "
        f"exited={counter.exited} "
        f"current={counter.current}"
    )


if __name__ == "__main__":

    ap = argparse.ArgumentParser(
        description="Real-time polygon-based entry/exit counter"
    )

    ap.add_argument("--tracks", help="tracking.json replay")
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)

    ap.add_argument(
        "--outside",
        type=parse_polygon,
        default=DEFAULT_OUTSIDE,
    )

    ap.add_argument(
        "--door",
        type=parse_polygon,
        default=DEFAULT_DOOR,
    )

    ap.add_argument(
        "--inside",
        type=parse_polygon,
        default=DEFAULT_INSIDE,
    )

    ap.add_argument("--anchor", choices=["bottom", "center"], default="bottom")
    ap.add_argument("--stable-frames", type=int, default=3)
    ap.add_argument("--door-timeout", type=int, default=90)
    ap.add_argument("--camera-id", default="entrance")

    args = ap.parse_args()

    if args.tracks:
        _replay(args.tracks, args)
    else:
        ap.print_help()