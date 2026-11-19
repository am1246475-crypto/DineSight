"""
cv/room_count.py  --  How many people are inside the restaurant (wide-angle hall camera)

Input  (per frame): tracks from Tracker.update()  [{"track_id": 7, "bbox": [x1, y1, x2, y2], ...}],
                    the frame shape, and the current time in seconds (video time for files,
                    wall time for streams)
Output (events, same shape as Contract 3), roughly every `interval_s` seconds:
    {"camera_id": "wide", "track_id": None, "event_type": "room_count",
     "timestamp": "2026-11-18T14:30:10.000Z",
     "meta": {"people": 12}}

How it works
------------
* The hall is ONE polygon in NORMALIZED coordinates (0..1). Default = the whole frame.
* We count TRACKS (not raw detections): the Tracker already removed duplicate boxes and gives
  every person one track_id. On top of that, each track_id is counted once per frame.
* A person is "in the room" when the anchor point of their box (center by default) is inside
  the polygon.
* Stabilizing (the raw count flickers when a person is missed for a moment or the tracker
  loses an ID):
    1. keep the raw counts of the last `window_s` seconds (default 4 s),
    2. take the MEDIAN (lower median: it never rounds up on an even split),
    3. once the window is full, the stable count can rise by at most `max_rise` (default 1) every `rise_interval_s`
       (default 1 s), so a sudden burst of ghost tracks can't inflate the number.
       Decreases follow the median directly.
* Times are in SECONDS, so the result does not depend on the FPS or on --frame-skip.
* The first event is sent after `window_s` seconds (so the median means something), then one
  every `interval_s` seconds (default 10 s).

Interface (shared with TableMonitor, so both can run on the same Tracker output in --mode wide):
    events = counter.update(tracks, frame.shape, ts)

Usage
-----
    python room_count.py --selftest
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
from statistics import median_low
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

try:
    from counting import parse_polygon
except ImportError:
    from src.cv.counting import parse_polygon

Point = Tuple[float, float]
Polygon = List[Point]

DEFAULT_ROOM: Polygon = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]     # the whole frame


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def load_room_file(path: str) -> Polygon:
    """JSON file: [[x, y], [x, y], ...]  or  {"room": [[x, y], ...]}  (normalized 0..1)"""
    import json

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict):
        raw = raw.get("room")
    if not isinstance(raw, list):
        raise ValueError('room file must be [[x, y], ...] or {"room": [[x, y], ...]}')
    poly = [(float(x), float(y)) for x, y in raw]
    if len(poly) < 3 or not all(0.0 <= v <= 1.0 for p in poly for v in p):
        raise ValueError("room polygon: need >= 3 points with values between 0 and 1")
    return poly


class RoomCounter:
    def __init__(
        self,
        polygon: Optional[Polygon] = None,
        camera_id: str = "wide",
        anchor: str = "center",
        window_s: float = 4.0,
        interval_s: float = 10.0,
        max_rise: int = 1,
        rise_interval_s: float = 1.0,
    ) -> None:
        poly = list(polygon) if polygon else list(DEFAULT_ROOM)
        if len(poly) < 3 or not all(0.0 <= v <= 1.0 for p in poly for v in p):
            raise ValueError("room polygon: need >= 3 points with values between 0 and 1")
        if anchor not in ("bottom", "center"):
            raise ValueError("anchor must be 'bottom' or 'center'")
        if window_s <= 0 or interval_s <= 0:
            raise ValueError("window_s and interval_s must be > 0")

        self.camera_id = camera_id
        self.polygon: Polygon = poly
        self.anchor = anchor
        self.window_s = window_s
        self.interval_s = interval_s
        self.max_rise = max(1, int(max_rise))
        self.rise_interval_s = rise_interval_s

        self.raw_count = 0                      # people in the room in the LAST frame
        self.stable_count = 0                   # smoothed number (this is what we send)
        self._samples: Deque[Tuple[float, int]] = deque()
        self._first_ts: Optional[float] = None
        self._last_ts: Optional[float] = None
        self._last_rise_ts: Optional[float] = None
        self._next_emit_ts: Optional[float] = None
        self._px_cache: Dict[Tuple[int, int], np.ndarray] = {}

    # ------------------------------------------------------------------ helpers
    def _anchor_point(self, bbox: Sequence[float]) -> Point:
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2.0
        return (cx, y2) if self.anchor == "bottom" else (cx, (y1 + y2) / 2.0)

    def _pixel_poly(self, width: int, height: int) -> np.ndarray:
        key = (width, height)
        if key not in self._px_cache:
            self._px_cache[key] = np.array(
                [(int(x * width), int(y * height)) for x, y in self.polygon], dtype=np.int32
            )
        return self._px_cache[key]

    def inside_ids(self, tracks: Sequence[Dict[str, Any]], frame_shape: Sequence[int]) -> List[Any]:
        """track_ids currently inside the room (used by the preview)."""
        height, width = int(frame_shape[0]), int(frame_shape[1])
        poly = self._pixel_poly(width, height)
        out: List[Any] = []
        for t in tracks:
            if "bbox" not in t:
                continue
            px, py = self._anchor_point(t["bbox"])
            if cv2.pointPolygonTest(poly, (float(px), float(py)), False) >= 0:
                out.append(t.get("track_id"))
        return out

    def _count(self, tracks: Sequence[Dict[str, Any]], frame_shape: Sequence[int]) -> int:
        ids = self.inside_ids(tracks, frame_shape)
        with_id = {i for i in ids if i is not None}            # one person = one track_id
        return len(with_id) + sum(1 for i in ids if i is None)

    def _reset(self) -> None:
        self._samples.clear()
        self._first_ts = self._last_ts = self._last_rise_ts = self._next_emit_ts = None
        self.stable_count = 0

    def _event(self, people: int) -> Dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "track_id": None,
            "event_type": "room_count",
            "timestamp": utc_now_iso(),
            "meta": {"people": int(people)},
        }

    # ------------------------------------------------------------------ main API
    def update(
        self,
        tracks: Sequence[Dict[str, Any]],
        frame_shape: Sequence[int],
        ts: float,
    ) -> List[Dict[str, Any]]:
        """Feed one frame. `ts` is the time in seconds. Returns the events created by this frame."""
        if self._last_ts is not None and ts < self._last_ts:    # video restarted (--loop)
            self._reset()
        self._last_ts = ts
        if self._first_ts is None:
            self._first_ts = ts
            self._next_emit_ts = ts + self.window_s

        # 1) raw count of this frame + sliding window
        self.raw_count = self._count(tracks, frame_shape)
        self._samples.append((ts, self.raw_count))
        while self._samples and self._samples[0][0] < ts - self.window_s:
            self._samples.popleft()

        # 2) median of the window
        med = median_low(c for _, c in self._samples)

        # 3) rises are rate-limited (once the window is full), drops follow the median
        warm = ts - self._first_ts >= self.window_s
        if med <= self.stable_count or not warm:
            self.stable_count = med
        elif self._last_rise_ts is None or ts - self._last_rise_ts >= self.rise_interval_s:
            self.stable_count = min(med, self.stable_count + self.max_rise)
            self._last_rise_ts = ts

        # 4) one event every interval_s
        if ts >= self._next_emit_ts:
            while self._next_emit_ts <= ts:                      # skip missed slots, send ONE event
                self._next_emit_ts += self.interval_s
            return [self._event(self.stable_count)]
        return []


# ---------------------------------------------------------------------- self-test
SHAPE = (432, 768, 3)


def _person(x: float, y: float, tid: int) -> Dict[str, Any]:
    return {"track_id": tid, "bbox": [x - 20, y - 40, x + 20, y + 40], "conf": 0.9}     # center = (x, y)


def _run(counter: RoomCounter, tracks_at, t_end: float, dt: float = 0.2, shape=SHAPE):
    """tracks_at(t) -> list of tracks. Returns [(t, people), ...] for every room_count event."""
    out = []
    t = 0.0
    while t <= t_end:
        for ev in counter.update(tracks_at(t), shape, t):
            assert ev["event_type"] == "room_count" and ev["track_id"] is None
            assert ev["camera_id"] == "wide" and list(ev["meta"]) == ["people"]
            out.append((round(t, 1), ev["meta"]["people"]))
        t += dt
    return out


def _selftest() -> None:
    crowd = lambda n: [_person(100 + 40 * i, 200, i + 1) for i in range(n)]

    # 1) steady crowd: first event after the window (4 s), then every 10 s
    c = RoomCounter()
    evs = _run(c, lambda t: crowd(5), 35)
    assert [p for _, p in evs] == [5, 5, 5, 5], evs
    assert 3.9 <= evs[0][0] <= 4.3 and 13.9 <= evs[1][0] <= 14.3 and 23.9 <= evs[2][0] <= 24.3, evs

    # 2) a person lost for 1 s every 3 s does not change the number
    c = RoomCounter()
    evs = _run(c, lambda t: crowd(5)[:4] if int(t) % 3 == 2 else crowd(5), 35)
    assert {p for _, p in evs} == {5}, evs

    # 3) a 1 s burst of 6 ghost tracks does not leak into the sent number
    c = RoomCounter()
    evs = _run(c, lambda t: crowd(5) + [_person(50 + 40 * i, 300, 100 + i) for i in range(6)] if 12.0 <= t < 13.0 else crowd(5), 35)
    assert {p for _, p in evs} == {5}, evs

    # 4) a real change (5 -> 9 people at t=15) shows up, rising by at most 1 per second
    c = RoomCounter(window_s=4.0)
    seen = []
    t = 0.0
    while t <= 30:
        c.update(crowd(5) if t < 15 else crowd(9), SHAPE, t)
        seen.append((t, c.stable_count))
        t += 0.2
    late = [(t, v) for t, v in seen if t >= 5.0]
    assert seen[-1][1] == 9 and max(b - a for (_, a), (_, b) in zip(late, late[1:])) <= 1, seen[-1]

    # 5) people who leave: the number drops once they are gone for half the window
    c = RoomCounter()
    for k in range(int(20 / 0.2)):
        t = k * 0.2
        c.update(crowd(5) if t < 10 else crowd(2), SHAPE, t)
    assert c.stable_count == 2, c.stable_count

    # 6) one person with two tracks sharing one id is counted once; people outside the polygon are ignored
    c = RoomCounter(polygon=[(0.0, 0.0), (0.5, 0.0), (0.5, 1.0), (0.0, 1.0)])      # left half only (x < 384)
    tr = [_person(100, 200, 1), _person(105, 200, 1), _person(200, 200, 2), _person(600, 200, 3)]
    assert c.update(tr, SHAPE, 0.0) == [] and c.raw_count == 2, c.raw_count

    # 7) the same normalized polygon works at any resolution
    for w, h in ((768, 432), (1920, 1080), (640, 360)):
        c = RoomCounter(polygon=[(0.0, 0.0), (0.5, 0.0), (0.5, 1.0), (0.0, 1.0)])
        s = w / 768.0
        c.update([_person(100 * s, 200 * s, 1), _person(600 * s, 200 * s, 2)], (h, w, 3), 0.0)
        assert c.raw_count == 1, (w, h, c.raw_count)

    # 8) empty room sends 0 (it is a real reading), and a restarted video (ts goes back) resets the state
    c = RoomCounter()
    evs = _run(c, lambda t: [], 15)
    assert [p for _, p in evs] == [0, 0], evs
    c.update(crowd(3), SHAPE, 100.0)
    c.update(crowd(3), SHAPE, 1.0)
    assert c._first_ts == 1.0 and c.stable_count == 3      # fresh start after the restart

    # 9) config
    for bad in ([(0, 0), (1, 0)], [(0, 0), (2, 0), (1, 1)]):
        try:
            RoomCounter(polygon=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"should have failed: {bad}")

    print("selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Room people-count module")
    ap.add_argument("--selftest", action="store_true", help="run the built-in tests")
    cli = ap.parse_args()
    if cli.selftest:
        _selftest()
    else:
        ap.print_help()
