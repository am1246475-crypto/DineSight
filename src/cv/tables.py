"""
cv/tables.py  --  Table occupancy (Occupied / Available) for the wide-angle camera

Input  (per frame): tracks or detections  [{"bbox": [x1, y1, x2, y2], ...}], the frame shape,
                    and the current time in seconds (video time for files, wall time for streams)
Output (events, same shape as Contract 3):
    {"camera_id": "wide", "track_id": null, "event_type": "table_occupied" | "table_free",
     "timestamp": "2026-11-18T14:30:00.000Z",
     "meta": {"table_id": "T3", "people": 2, "video_s": 123.4}}

How it works
------------
* Each table is a polygon in NORMALIZED coordinates (0..1). Draw it around the table AND its
  chairs, because that is where the people's boxes will be.
* A person is "at a table" when the anchor point of their box (center by default; seated people are
  often partly hidden, so the box center is more reliable than the feet) is inside the polygon.
* Time-based hysteresis, so people just walking past do not flicker the status:
    - Available -> Occupied after someone stays at the table for `occupy_after_s` (default 5 s)
    - Occupied  -> Available after the table is empty for `free_after_s` (default 20 s)
    - Missed detections shorter than `gap_tolerance_s` (default 2 s) do not restart the timers.
* Times are in SECONDS, so the result does not depend on the FPS or on --frame-skip.
* A table that has never had an event is Available (no startup events are sent).
* A waiter who stands at an empty table longer than `occupy_after_s` will mark it occupied; that is
  expected with this method.

Usage
-----
    python tables.py --selftest
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

try:
    from counting import parse_polygon
except ImportError:
    from src.cv.counting import parse_polygon

Point = Tuple[float, float]
Polygon = List[Point]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------- config helpers
def parse_table(text: str) -> Tuple[str, Polygon]:
    """'T1=x1,y1;x2,y2;x3,y3;...' (normalized 0..1)  ->  ('T1', [(x1, y1), ...])"""
    if "=" not in text:
        raise argparse.ArgumentTypeError("table format: T1=x1,y1;x2,y2;x3,y3;...")
    name, poly = text.split("=", 1)
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError("table name is empty (example: T1=0.1,0.2;0.3,0.2;0.3,0.4)")
    return name, parse_polygon(poly.strip())


def load_tables_file(path: str) -> Dict[str, Polygon]:
    """JSON file: {"T1": [[x, y], [x, y], ...], "T2": [...]}  (normalized 0..1)"""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    tables: Dict[str, Polygon] = {}
    for name, pts in raw.items():
        poly = [(float(x), float(y)) for x, y in pts]
        if len(poly) < 3 or not all(0.0 <= v <= 1.0 for p in poly for v in p):
            raise ValueError(f"table {name!r}: need >= 3 points with values between 0 and 1")
        tables[str(name)] = poly
    if not tables:
        raise ValueError("tables file is empty")
    return tables


# ---------------------------------------------------------------------- monitor
class TableMonitor:
    def __init__(
        self,
        tables: Dict[str, Polygon],
        camera_id: str = "wide",
        anchor: str = "center",
        occupy_after_s: float = 5.0,
        free_after_s: float = 20.0,
        gap_tolerance_s: float = 2.0,
    ) -> None:
        if not tables:
            raise ValueError("at least one table is required")
        if anchor not in ("bottom", "center"):
            raise ValueError("anchor must be 'bottom' or 'center'")
        self.camera_id = camera_id
        self.polygons: Dict[str, Polygon] = dict(tables)
        self.anchor = anchor
        self.occupy_after_s = occupy_after_s
        self.free_after_s = free_after_s
        self.gap_tolerance_s = gap_tolerance_s

        self._state: Dict[str, Dict[str, Any]] = {
            name: {
                "status": "available",
                "since": None,          # time of the last status change
                "people": 0,
                "present_since": None,  # start of the current continuous presence
                "last_present": None,   # last time someone was seen at the table
                "absent_since": None,   # start of the current absence (when it is long enough to count)
            }
            for name in self.polygons
        }
        self._px_cache: Dict[Tuple[int, int], Dict[str, np.ndarray]] = {}

    # ------------------------------------------------------------------ helpers
    def _anchor_point(self, bbox: Sequence[float]) -> Point:
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2.0
        return (cx, y2) if self.anchor == "bottom" else (cx, (y1 + y2) / 2.0)

    def _pixel_polys(self, width: int, height: int) -> Dict[str, np.ndarray]:
        key = (width, height)
        if key not in self._px_cache:
            self._px_cache[key] = {
                name: np.array([(int(x * width), int(y * height)) for x, y in poly], dtype=np.int32)
                for name, poly in self.polygons.items()
            }
        return self._px_cache[key]

    def status(self) -> Dict[str, Dict[str, Any]]:
        """Current status of every table (used by the preview)."""
        return {
            name: {"status": st["status"], "since": st["since"], "people": st["people"]}
            for name, st in self._state.items()
        }

    @property
    def occupied_count(self) -> int:
        return sum(1 for st in self._state.values() if st["status"] == "occupied")

    def _event(self, name: str, new_status: str, ts: float, people: int) -> Dict[str, Any]:
        st = self._state[name]
        st["status"] = new_status
        st["since"] = ts
        return {
            "camera_id": self.camera_id,
            "track_id": None,
            "event_type": "table_occupied" if new_status == "occupied" else "table_free",
            "timestamp": utc_now_iso(),
            "meta": {"table_id": name, "people": people, "video_s": round(ts, 1)},
        }

    # ------------------------------------------------------------------ main API
    def update(
        self,
        tracks: Sequence[Dict[str, Any]],
        frame_shape: Sequence[int],
        ts: float,
    ) -> List[Dict[str, Any]]:
        """Feed one frame. `ts` is the time in seconds. Returns the events created by this frame."""
        height, width = int(frame_shape[0]), int(frame_shape[1])
        polys = self._pixel_polys(width, height)
        points = [self._anchor_point(t["bbox"]) for t in tracks if "bbox" in t]

        events: List[Dict[str, Any]] = []
        for name, poly in polys.items():
            st = self._state[name]
            n = sum(1 for (px, py) in points if cv2.pointPolygonTest(poly, (float(px), float(py)), False) >= 0)
            st["people"] = n

            if n > 0:
                st["last_present"] = ts
                st["absent_since"] = None
                if st["present_since"] is None:
                    st["present_since"] = ts
                if st["status"] == "available" and ts - st["present_since"] >= self.occupy_after_s:
                    events.append(self._event(name, "occupied", ts, n))
                continue

            # nobody at the table right now
            last = st["last_present"]
            if last is not None and ts - last <= self.gap_tolerance_s:
                continue                                    # short gap (missed detection): ignore
            st["present_since"] = None
            if st["absent_since"] is None:
                st["absent_since"] = last if last is not None else ts
            if st["status"] == "occupied" and ts - st["absent_since"] >= self.free_after_s:
                events.append(self._event(name, "available", ts, 0))

        return events


# ---------------------------------------------------------------------- self-test
SHAPE = (432, 768, 3)
T1: Polygon = [(0.4, 0.4), (0.6, 0.4), (0.6, 0.7), (0.4, 0.7)]       # pixels: x 307-461, y 173-302
T2: Polygon = [(0.7, 0.4), (0.9, 0.4), (0.9, 0.7), (0.7, 0.7)]       # pixels: x 538-691, y 173-302


def _person(x: float, y: float) -> Dict[str, Any]:
    return {"bbox": [x - 20, y - 40, x + 20, y + 40], "conf": 0.9}      # center = (x, y)


def _run(monitor: TableMonitor, present, t_end: float, dt: float = 0.2, shape=SHAPE, scale: float = 1.0):
    """present(t) -> list of (x, y) in 768x432 coordinates. Returns [(t, event), ...]."""
    out = []
    t = 0.0
    while t <= t_end:
        tracks = [_person(x * scale, y * scale) for x, y in present(t)]
        for ev in monitor.update(tracks, shape, t):
            out.append((round(t, 1), ev))
        t += dt
    return out


def _types(evs) -> List[str]:
    return [e["event_type"] for _, e in evs]


def _selftest() -> None:
    P1 = (384.0, 260.0)       # inside T1
    P2 = (600.0, 260.0)       # inside T2

    # 1) someone sits from 3 s to 13 s: occupied after 5 s, free 20 s after they leave
    m = TableMonitor({"T1": T1})
    evs = _run(m, lambda t: [P1] if 3.0 <= t < 13.0 else [], 60)
    assert _types(evs) == ["table_occupied", "table_free"], evs
    assert 7.9 <= evs[0][0] <= 8.3, evs
    assert 32.5 <= evs[1][0] <= 33.3, evs
    assert evs[0][1]["meta"]["table_id"] == "T1" and evs[0][1]["track_id"] is None

    # 2) a 2-second walk-by does not change anything
    m = TableMonitor({"T1": T1})
    assert _run(m, lambda t: [P1] if 3.0 <= t < 5.0 else [], 40) == []

    # 3) detection drop-outs of 1 s (shorter than the tolerance) do not restart the timer
    m = TableMonitor({"T1": T1})
    evs = _run(m, lambda t: [] if (int(t) % 3 == 2) else [P1], 40)
    assert _types(evs) == ["table_occupied"], evs
    assert evs[0][0] <= 8.5, evs

    # 4) the guests leave and come back within 20 s: the table stays occupied
    m = TableMonitor({"T1": T1})
    evs = _run(m, lambda t: [P1] if (t < 10.0 or 25.0 <= t < 40.0) else [], 80)
    assert _types(evs) == ["table_occupied", "table_free"], evs
    assert evs[1][0] > 59.0, evs                         # freed only 20 s after the second departure (t=40)

    # 5) two tables are independent, and the number of people is reported
    m = TableMonitor({"T1": T1, "T2": T2})
    evs = _run(m, lambda t: [P1] + [P2, (610.0, 270.0)] if t >= 1.0 else [], 20)
    got = {e["meta"]["table_id"]: e["meta"]["people"] for _, e in evs if e["event_type"] == "table_occupied"}
    assert got == {"T1": 1, "T2": 2}, got
    assert m.occupied_count == 2 and m.status()["T2"]["status"] == "occupied"

    # 6) the same normalized positions work at any resolution
    for w, h in ((768, 432), (1920, 1080), (640, 360)):
        m = TableMonitor({"T1": T1})
        s = w / 768.0
        evs = _run(m, lambda t: [P1] if t >= 1.0 else [], 10, shape=(h, w, 3), scale=s)
        assert _types(evs) == ["table_occupied"], (w, h, evs)

    # 7) a person standing between the tables is ignored
    m = TableMonitor({"T1": T1, "T2": T2})
    assert _run(m, lambda t: [(500.0, 260.0)], 30) == []

    # 8) config parsing
    name, poly = parse_table("T9=0.1,0.1;0.3,0.1;0.3,0.3")
    assert name == "T9" and len(poly) == 3
    for bad in ("T1", "=0,0;1,0;1,1", "T1=0,0;1,0", "T1=0,0;2,0;1,1"):
        try:
            parse_table(bad)
        except argparse.ArgumentTypeError:
            pass
        else:
            raise AssertionError(f"should have failed: {bad}")

    print("selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Table occupancy module")
    ap.add_argument("--selftest", action="store_true", help="run the built-in tests")
    cli = ap.parse_args()
    if cli.selftest:
        _selftest()
    else:
        ap.print_help()
