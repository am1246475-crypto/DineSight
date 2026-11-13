from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]
Line = Tuple[Point, Point]

DEFAULT_LINE: Line = ((0.25, 0.0), (0.25, 1.0))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_line(text: str) -> Line:
    """'x1,y1,x2,y2' (normalized 0..1) -> ((x1, y1), (x2, y2))."""
    try:
        x1, y1, x2, y2 = [float(v) for v in text.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("line must look like 'x1,y1,x2,y2' (numbers from 0 to 1)")
    if not all(0.0 <= v <= 1.0 for v in (x1, y1, x2, y2)):
        raise argparse.ArgumentTypeError("line values must be normalized (between 0 and 1)")
    if (x1, y1) == (x2, y2):
        raise argparse.ArgumentTypeError("line start and end must be different points")
    return ((x1, y1), (x2, y2))


class LineCounter:
    def __init__(
        self,
        camera_id: str = "entrance",
        line: Line = DEFAULT_LINE,
        margin_px: float = 8.0,
        anchor: str = "bottom",
        invert: bool = False,
        edge_tol: float = 0.1,
        state_ttl_frames: int = 300,
    ) -> None:
        if anchor not in ("bottom", "center"):
            raise ValueError("anchor must be 'bottom' or 'center'")
        self.camera_id = camera_id
        self.line = line
        self.margin_px = margin_px
        self.anchor = anchor
        self.invert = invert
        self.edge_tol = edge_tol          # how far past the line ends a crossing still counts
        self.state_ttl_frames = state_ttl_frames

        self.entered = 0
        self.exited = 0
        self._state: Dict[int, Dict[str, int]] = {}   # track_id -> {"side": -1|0|1, "last_seen": n}
        self._frame_no = 0

    # ------------------------------------------------------------------ helpers
    @property
    def current(self) -> int:
        """Local estimate for the on-screen overlay. The backend is the source of truth."""
        return max(0, self.entered - self.exited)

    def _anchor_point(self, bbox: Sequence[float]) -> Point:
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2.0
        return (cx, y2) if self.anchor == "bottom" else (cx, (y1 + y2) / 2.0)

    def _geometry(self, width: int, height: int):
        (ax, ay), (bx, by) = self.line
        a = (ax * width, ay * height)
        b = (bx * width, by * height)
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy) or 1e-9
        nx, ny = dy / length, -dx / length
        if self.invert:
            nx, ny = -nx, -ny
        return a, b, dx, dy, length, nx, ny

    def inside_normal(self, width: int, height: int) -> Point:
        """Unit vector pointing to the 'inside' side (used to draw the arrow)."""
        *_, nx, ny = self._geometry(width, height)
        return nx, ny

    # ------------------------------------------------------------------ main API
    def update(
        self,
        tracks: Sequence[Dict[str, Any]],
        frame_shape: Sequence[int],
        frame_idx: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Feed the tracks of one frame. Returns the events created by this frame."""
        height, width = int(frame_shape[0]), int(frame_shape[1])
        self._frame_no = frame_idx if frame_idx is not None else self._frame_no + 1
        a, _b, dx, dy, length, nx, ny = self._geometry(width, height)

        events: List[Dict[str, Any]] = []
        for trk in tracks:
            if "track_id" not in trk or "bbox" not in trk:
                continue
            tid = int(trk["track_id"])
            px, py = self._anchor_point(trk["bbox"])

            dist = (px - a[0]) * nx + (py - a[1]) * ny                       # signed distance to the line
            t = ((px - a[0]) * dx + (py - a[1]) * dy) / (length * length)   # 0..1 along the segment

            st = self._state.setdefault(tid, {"side": 0, "last_seen": self._frame_no})
            st["last_seen"] = self._frame_no

            if abs(dist) < self.margin_px:      # inside the dead band: keep the previous side
                continue

            side = 1 if dist > 0 else -1
            prev = st["side"]
            st["side"] = side

            if prev and prev != side and -self.edge_tol <= t <= 1.0 + self.edge_tol:
                event_type = "enter" if side > 0 else "exit"
                if event_type == "enter":
                    self.entered += 1
                else:
                    self.exited += 1
                events.append(
                    {
                        "camera_id": self.camera_id,
                        "track_id": tid,
                        "event_type": event_type,
                        "timestamp": utc_now_iso(),
                        "meta": {"frame": self._frame_no, "anchor": [round(px, 1), round(py, 1)]},
                    }
                )

        # forget tracks that have not been seen for a long time
        old = [k for k, v in self._state.items() if self._frame_no - v["last_seen"] > self.state_ttl_frames]
        for k in old:
            del self._state[k]

        return events


# ---------------------------------------------------------------------- tests / tools
def _track(tid: int, x: float, foot_y: float = 400.0) -> Dict[str, Any]:
    return {"track_id": tid, "bbox": [x - 20, foot_y - 100, x + 20, foot_y], "conf": 0.9}


def _types(events: List[Dict[str, Any]]) -> List[str]:
    return [e["event_type"] for e in events]


def _selftest() -> None:
    shape = (432, 768, 3)  # default line is at x = 0.25 * 768 = 192

    # 1) walk right (enter), then walk back left (exit)
    c = LineCounter()
    evs: List[Dict[str, Any]] = []
    for x in range(100, 300, 5):
        evs += c.update([_track(1, x)], shape)
    assert _types(evs) == ["enter"], evs
    evs = []
    for x in range(300, 100, -5):
        evs += c.update([_track(1, x)], shape)
    assert _types(evs) == ["exit"], evs
    assert (c.entered, c.exited, c.current) == (1, 1, 0)

    # 2) jitter on the line is not counted; the real crossing is counted once
    c = LineCounter()
    evs = []
    for x in (150, 160, 170):
        evs += c.update([_track(2, x)], shape)
    for i in range(60):
        evs += c.update([_track(2, 192 + (3 if i % 2 else -3))], shape)
    assert evs == [], evs
    for x in (230, 250):
        evs += c.update([_track(2, x)], shape)
    assert _types(evs) == ["enter"], evs

    # 3) invert flips the meaning of the two sides
    c = LineCounter(invert=True)
    evs = []
    for x in range(100, 300, 5):
        evs += c.update([_track(1, x)], shape)
    assert _types(evs) == ["exit"], evs

    # 4) two people crossing in opposite directions are counted independently
    c = LineCounter()
    evs = []
    xs_in = list(range(100, 300, 5))
    xs_out = list(range(300, 100, -5))
    for xi, xo in zip(xs_in, xs_out):
        evs += c.update([_track(3, xi), _track(4, xo)], shape)
    assert sorted(_types(evs)) == ["enter", "exit"], evs

    # 5) crossing the line's extension (outside the segment) is ignored
    c = LineCounter(line=((0.25, 0.0), (0.25, 0.3)))
    evs = []
    for x in range(100, 300, 5):
        evs += c.update([_track(1, x)], shape)
    assert evs == [], evs

    # 6) old track states are cleaned up
    c = LineCounter(state_ttl_frames=10)
    c.update([_track(9, 100)], shape)
    for _ in range(20):
        c.update([], shape)
    assert c._state == {}

    print("selftest OK")


def _replay(path: str, args: argparse.Namespace) -> None:
    """Run the counting logic on a tracking.json file (no models needed)."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    meta: Dict[str, Any] = {}
    frames = raw
    if isinstance(raw, dict):
        meta = raw.get("meta", {}) or {}
        frames = raw.get("frames", [])

    width = args.width or meta.get("width")
    height = args.height or meta.get("height")
    if not width or not height:
        raise SystemExit("tracking JSON has no 'meta' with width/height: pass --width and --height")

    counter = LineCounter(
        camera_id=args.camera_id, line=args.line, margin_px=args.margin,
        anchor=args.anchor, invert=args.invert,
    )
    for i, fr in enumerate(frames):
        if isinstance(fr, list):
            tracks, idx = fr, i
        else:
            tracks = fr.get("detections", fr.get("tracks", []))
            idx = fr.get("frame_id", fr.get("frame", i))
        for ev in counter.update(tracks, (int(height), int(width)), frame_idx=idx):
            print(json.dumps(ev))
    print(f"frames={len(frames)}  entered={counter.entered}  exited={counter.exited}  current={counter.current}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Entry/exit line counter: self-test or replay of tracking.json")
    ap.add_argument("--selftest", action="store_true", help="run the built-in tests")
    ap.add_argument("--tracks", help="tracking.json to replay")
    ap.add_argument("--line", type=parse_line, default=DEFAULT_LINE, help="x1,y1,x2,y2 normalized (default 0.25,0,0.25,1)")
    ap.add_argument("--invert", action="store_true", help="swap which side is 'inside'")
    ap.add_argument("--anchor", choices=["bottom", "center"], default="bottom")
    ap.add_argument("--margin", type=float, default=8.0, help="dead band in pixels around the line")
    ap.add_argument("--camera-id", default="entrance")
    ap.add_argument("--width", type=int, help="frame width if tracking.json has no meta")
    ap.add_argument("--height", type=int, help="frame height if tracking.json has no meta")
    cli = ap.parse_args()

    if cli.selftest:
        _selftest()
    elif cli.tracks:
        _replay(cli.tracks, cli)
    else:
        ap.print_help()