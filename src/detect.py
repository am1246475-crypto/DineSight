"""
DineSight Vision Pipeline (src/detect.py)

People detection + tracking (YOLO11 + ByteTrack), doorway entry/exit counting,
table occupancy and customer/staff classification.

Everything that used to be hard-coded is now derived from the video itself:
  * fps / resolution  -> all thresholds are in seconds or relative to body size
  * tables            -> auto-discovered from persistent tables + chairs
  * doorway + entry   -> auto-estimated from where people appear / disappear
                         (or taken from a profile / --line / --entry-dir)
"""

from __future__ import annotations

import argparse
import json
import copy
import math
import sys
from collections import deque
from dataclasses import dataclass, fields as dataclass_fields
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
from ultralytics import YOLO

# Ensure src can import count
CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

try:
    from count import DineSightCounter, signed_side, distance_to_segment
except ImportError:
    from src.count import DineSightCounter, signed_side, distance_to_segment

# Configuration Defaults
DEFAULT_VIDEO = Path("videos/case.mp4")
DEFAULT_OUTPUT = Path("videos/detected_case.mp4")
MODEL_PATH = "yolo11s.pt"
TRACKER_CONFIG = "bytetrack_custom.yaml"
PROFILES_DIR = Path("profiles")

PERSON, CHAIR, TABLE = 0, 56, 60  # COCO class ids
SPAWN_ENTRY = True  # count people first detected inside the door zone (disable with --no-spawn-entry)
VALID_DIRECTIONS = ["left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top"]

Source = Union[str, int, Path]
Box = Tuple[int, int, int, int]


def resolve_tracker_config() -> str:
    for cand in (Path(TRACKER_CONFIG), CURRENT_DIR.parent / TRACKER_CONFIG, CURRENT_DIR / TRACKER_CONFIG):
        if cand.exists():
            return str(cand)
    return "bytetrack.yaml"


# Scene Configuration Loader

def load_scene_profile(
    profile_name_or_path: Optional[str],
    video_path: Path,
    width: int,
    height: int,
) -> Tuple[Optional[Tuple[Tuple[int, int], Tuple[int, int]]], str, List[Dict]]:
    """
    Loads scene configuration (doorway line, entry direction, table zones).
    Priority:
    1. Explicit profile path or name passed via CLI (--profile).
    2. A profile named after the video file (profiles/<video-stem>.json).
    3. Nothing -> (None, "left_to_right", []) which triggers auto-discovery.
    """
    config_data = None

    if profile_name_or_path:
        p_path = Path(profile_name_or_path)
        if not p_path.exists():
            p_path = PROFILES_DIR / f"{profile_name_or_path}.json"
        if p_path.exists():
            with open(p_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
            print(f"[Config] Loaded profile from: {p_path.resolve()}")
        else:
            print(f"[Config Warning] Profile '{profile_name_or_path}' not found.")

    if config_data is None:
        candidate = PROFILES_DIR / f"{video_path.stem.lower()}.json"
        if candidate.exists():
            with open(candidate, "r", encoding="utf-8") as f:
                config_data = json.load(f)
            print(f"[Config] Automatically resolved matching profile: {candidate.name}")

    if not config_data:
        return None, "left_to_right", []

    line = None
    if "line_start" in config_data and "line_end" in config_data:
        ls, le = config_data["line_start"], config_data["line_end"]
        line = (
            (int(ls[0] * width), int(ls[1] * height)),
            (int(le[0] * width), int(le[1] * height)),
        )

    entry_direction = config_data.get("entry_direction", "left_to_right")

    tables = []
    for t in config_data.get("tables", []):
        bx = t["box_ratio"]
        tables.append({
            "id": t["id"],
            "name": t.get("name", f"Table {t['id']}"),
            "box": (int(bx[0] * width), int(bx[1] * height), int(bx[2] * width), int(bx[3] * height)),
            "capacity": t.get("capacity", 4),
        })

    return line, entry_direction, tables


def _find_profile_path(profile_name_or_path: Optional[str], video_path: Path) -> Optional[Path]:
    """Same lookup order as load_scene_profile, but only returns the file path."""
    if profile_name_or_path:
        p_path = Path(profile_name_or_path)
        if not p_path.exists():
            p_path = PROFILES_DIR / f"{profile_name_or_path}.json"
        if p_path.exists():
            return p_path
    candidate = PROFILES_DIR / f"{video_path.stem.lower()}.json"
    return candidate if candidate.exists() else None


def load_profile_analytics(profile_name_or_path: Optional[str], video_path: Path) -> Dict[str, float]:
    """
    Per-camera classification settings from the profile's optional "analytics" block, e.g.

        "analytics": {"staff_observe_s": 8, "customer_dwell_s": 4, "warmup_s": 5}

    Only AnalyticsConfig field names with non-negative numbers are accepted; anything else is
    ignored with a warning, so a typo in a profile never crashes a run.
    """
    path = _find_profile_path(profile_name_or_path, video_path)
    if path is None:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = (json.load(f) or {}).get("analytics")
    except (OSError, ValueError):
        return {}
    if not raw:
        return {}
    if not isinstance(raw, dict):
        print(f"[Config Warning] 'analytics' in {path.name} must be an object: ignored.")
        return {}
    valid = {f.name for f in dataclass_fields(AnalyticsConfig)}
    out: Dict[str, float] = {}
    for key, value in raw.items():
        if key not in valid:
            print(f"[Config Warning] Unknown analytics setting '{key}' in {path.name}: ignored "
                  f"(valid: {', '.join(sorted(valid))}).")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            print(f"[Config Warning] Analytics setting '{key}' in {path.name} must be a number >= 0: ignored.")
        else:
            out[key] = float(value)
    return out



# Staff uniform colour (optional, per camera / profile)

@dataclass
class UniformConfig:
    """Staff uniform colour(s) in OpenCV HSV (H 0-179, S/V 0-255), sampled by clicking on a staff member."""
    samples: List[Tuple[float, float, float]]
    hue_tol: float = 12.0        # circular hue distance (ignored for grey/black/white samples)
    sat_tol: float = 70.0
    val_tol: float = 70.0
    pixel_ratio: float = 0.30    # share of torso pixels that must match for one observation to count as a hit
    min_samples: int = 8         # observations needed before a track can be judged
    confirm_ratio: float = 0.6   # share of hits that confirms STAFF
    strict: bool = False         # True: once judged "no match", behaviour alone can no longer label STAFF


def load_profile_uniform(profile_name_or_path: Optional[str], video_path: Path) -> Optional[UniformConfig]:
    """Reads the optional "staff_uniform" block of the profile; invalid input is ignored with a warning."""
    path = _find_profile_path(profile_name_or_path, video_path)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = (json.load(f) or {}).get("staff_uniform")
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    if not isinstance(raw, dict):
        print(f"[Config Warning] 'staff_uniform' in {path.name} must be an object: ignored.")
        return None
    samples: List[Tuple[float, float, float]] = []
    for item in raw.get("samples", []):
        ok = (
            isinstance(item, (list, tuple)) and len(item) == 3
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in item)
            and 0 <= item[0] <= 179 and 0 <= item[1] <= 255 and 0 <= item[2] <= 255
        )
        if ok:
            samples.append((float(item[0]), float(item[1]), float(item[2])))
        else:
            print(f"[Config Warning] Bad uniform sample {item!r} in {path.name}: ignored (need [H 0-179, S 0-255, V 0-255]).")
    if not samples:
        return None
    cfg = UniformConfig(samples=samples[:6])
    for key in ("hue_tol", "sat_tol", "val_tol", "pixel_ratio", "confirm_ratio"):
        if isinstance(raw.get(key), (int, float)) and not isinstance(raw.get(key), bool) and raw[key] >= 0:
            setattr(cfg, key, float(raw[key]))
    if isinstance(raw.get("min_samples"), int) and raw["min_samples"] >= 1:
        cfg.min_samples = raw["min_samples"]
    cfg.strict = bool(raw.get("strict", False))
    return cfg


def uniform_match_ratio(frame: np.ndarray, box: Tuple[float, float, float, float], uni: UniformConfig) -> Optional[float]:
    """Share (0-1) of the person's torso pixels that are close to a uniform sample; None if the box is too small."""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    if w < 8 or h < 16:
        return None
    H, W = frame.shape[:2]
    rx1, rx2 = max(0, int(x1 + 0.25 * w)), min(W, int(x2 - 0.25 * w))
    ry1, ry2 = max(0, int(y1 + 0.20 * h)), min(H, int(y1 + 0.55 * h))   # chest / torso band
    if rx2 - rx1 < 4 or ry2 - ry1 < 4:
        return None
    hsv = cv2.cvtColor(frame[ry1:ry2, rx1:rx2], cv2.COLOR_BGR2HSV).astype(np.int16)
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    mask = np.zeros(hue.shape, dtype=bool)
    for sh, ss, sv in uni.samples:
        m = (np.abs(sat - ss) <= uni.sat_tol) & (np.abs(val - sv) <= uni.val_tol)
        if ss >= 40:   # coloured sample: hue matters (circular); grey/black/white: hue is noise
            dh = np.abs(hue - sh)
            m &= np.minimum(dh, 180 - dh) <= uni.hue_tol
        mask |= m
    return float(mask.mean())


# Geometry helpers

def _iou_matrix(boxes: np.ndarray) -> np.ndarray:
    ix1 = np.maximum(boxes[:, None, 0], boxes[None, :, 0])
    iy1 = np.maximum(boxes[:, None, 1], boxes[None, :, 1])
    ix2 = np.minimum(boxes[:, None, 2], boxes[None, :, 2])
    iy2 = np.minimum(boxes[:, None, 3], boxes[None, :, 3])
    inter = np.clip(ix2 - ix1, 0, None) * np.clip(iy2 - iy1, 0, None)
    area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    union = area[:, None] + area[None, :] - inter
    return inter / np.maximum(union, 1e-9)


def _box_gap(a, b) -> float:
    """Distance between two boxes (0 when they overlap)."""
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _group_by_iou(dets: List[tuple], thr: float = 0.35) -> List[List[tuple]]:
    """
    Group detections (frame_idx, cls, box) that are the same static object seen in
    different frames.  Two boxes from the SAME frame are different objects, never merged.
    """
    if not dets:
        return []
    boxes = np.array([d[2] for d in dets], dtype=float)
    iou = _iou_matrix(boxes)
    parent = list(range(len(dets)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    ii, jj = np.where(np.triu(iou, 1) > thr)
    for i, j in zip(ii.tolist(), jj.tolist()):
        if dets[i][0] == dets[j][0]:
            continue
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    groups: Dict[int, List[tuple]] = {}
    for i, d in enumerate(dets):
        groups.setdefault(find(i), []).append(d)
    return list(groups.values())


def pick_table_zone(box: Tuple[float, float, float, float], tables: List[Dict]) -> Optional[int]:
    """Zone a person belongs to: centre inside the zone, or a large share of the body overlapping it."""
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    area = max(1.0, (x2 - x1) * (y2 - y1))
    best_id, best_score = None, 0.0
    for t in tables:
        bx1, by1, bx2, by2 = t["box"]
        ix = max(0.0, min(x2, bx2) - max(x1, bx1))
        iy = max(0.0, min(y2, by2) - max(y1, by1))
        overlap = ix * iy / area
        inside = bx1 <= cx <= bx2 and by1 <= cy <= by2
        if inside or overlap >= 0.35:
            score = overlap + (0.5 if inside else 0.0)
            if score > best_score:
                best_id, best_score = t["id"], score
    return best_id



# Dynamic Table Auto-Discovery
def sample_video_frames(cap: cv2.VideoCapture, n: int = 35) -> List[np.ndarray]:
    """Evenly spaced frames of a file (then rewinds). For live streams, grabs n frames spread over a few seconds."""
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frames: List[np.ndarray] = []
    if total > 0:
        for idx in np.linspace(0, total - 1, min(n, total)).astype(int).tolist():
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
            ok, frame = cap.read()
            if ok:
                frames.append(frame)
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    else:  # live stream: cannot seek; these frames are consumed
        for _ in range(min(n, 20)):
            for _ in range(3):
                cap.grab()
            ok, frame = cap.read()
            if ok:
                frames.append(frame)
    return frames


def build_table_zones(dets: List[tuple], n_frames: int, width: int, height: int) -> List[Dict]:
    """
    Turn raw table/chair detections (frame_idx, cls, box) from sampled frames into table zones.

    1. Static objects are found by grouping detections across frames; an object must be
       seen in >=15% of the sampled frames (people walking past do not create tables).
    2. Each detected table becomes a zone; chairs close to a table join it.
    3. Chairs with no table (tables are often missed by YOLO) form their own zone
       when 2+ chairs sit next to each other.
    4. Zone boxes are padded proportionally (seated people lean outside the furniture).
    5. Capacity = number of chairs found (min 2), else estimated from the table size.
    """
    if n_frames <= 0 or not dets:
        return []

    min_seen = max(2, int(math.ceil(0.15 * n_frames)))
    objects: Dict[int, List[np.ndarray]] = {CHAIR: [], TABLE: []}
    for cls in (CHAIR, TABLE):
        subset = [d for d in dets if d[1] == cls]
        for g in _group_by_iou(subset, 0.35):
            if len({d[0] for d in g}) >= min_seen:
                objects[cls].append(np.median(np.array([d[2] for d in g], dtype=float), axis=0))

    zones: List[Dict] = [{"boxes": [t], "chairs": 0, "has_table": True} for t in objects[TABLE]]

    orphans: List[np.ndarray] = []
    for chair in objects[CHAIR]:
        best, best_gap = None, None
        for z in zones:
            tb = z["boxes"][0]
            gap = _box_gap(chair, tb)
            limit = 0.6 * max(tb[2] - tb[0], tb[3] - tb[1])
            if gap <= limit and (best_gap is None or gap < best_gap):
                best, best_gap = z, gap
        if best is None:
            orphans.append(chair)
        else:
            best["boxes"].append(chair)
            best["chairs"] += 1

    groups: List[List[np.ndarray]] = []
    for chair in orphans:
        size = max(chair[2] - chair[0], chair[3] - chair[1])
        near = [g for g in groups if any(_box_gap(chair, o) <= 0.8 * size for o in g)]
        merged = [chair]
        for g in near:
            merged.extend(g)
        groups = [g for g in groups if all(g is not n for n in near)]
        groups.append(merged)
    for g in groups:
        if len(g) >= 2:
            zones.append({"boxes": list(g), "chairs": len(g), "has_table": False})

    result = []
    for z in zones:
        arr = np.array(z["boxes"], dtype=float)
        x1, y1, x2, y2 = arr[:, 0].min(), arr[:, 1].min(), arr[:, 2].max(), arr[:, 3].max()
        w, h = x2 - x1, y2 - y1
        px, py = max(8.0, 0.10 * w), max(8.0, 0.10 * h)
        box = (
            max(0, int(x1 - px)), max(0, int(y1 - py)),
            min(width, int(x2 + px)), min(height, int(y2 + py)),
        )
        if z["chairs"] >= 1:
            capacity = int(min(10, max(2, z["chairs"])))
        else:
            capacity = 4 if (w * h) > (width * height * 0.04) else 2
        result.append({"box": box, "capacity": capacity})

    result.sort(key=lambda r: (round(((r["box"][1] + r["box"][3]) / 2) / (0.12 * height)), r["box"][0]))
    for i, r in enumerate(result):
        r["id"] = i + 1
        r["name"] = f"Table {i + 1}"
    return result


def discover_tables_dynamically(
    frames: List[np.ndarray],
    model: YOLO,
    width: int,
    height: int,
    conf: float = 0.18,
) -> List[Dict]:
    """Detect tables and chairs on sampled frames and build zones from the persistent ones."""
    dets: List[tuple] = []
    for i, frame in enumerate(frames):
        res = model(frame, classes=[CHAIR, TABLE], conf=conf, verbose=False)[0]
        if res.boxes is None or len(res.boxes) == 0:
            continue
        for b, c in zip(res.boxes.xyxy.cpu().numpy(), res.boxes.cls.int().cpu().tolist()):
            dets.append((i, int(c), b))
    return build_table_zones(dets, len(frames), width, height)



# Dynamic Doorway Auto-Discovery

def _clip_segment(center: np.ndarray, direction: np.ndarray, half: float, width: int, height: int):
    center = np.clip(center, [0, 0], [width - 1, height - 1])

    def reach(sign: float) -> float:
        t = half
        for axis, limit in ((0, width - 1), (1, height - 1)):
            d = direction[axis] * sign
            if abs(d) < 1e-9:
                continue
            bound = limit if d > 0 else 0
            t = min(t, (bound - center[axis]) / d)
        return max(t, 0.0)

    a = center - direction * reach(-1.0)
    b = center + direction * reach(1.0)
    return (int(a[0]), int(a[1])), (int(b[0]), int(b[1]))


def estimate_doorway(
    tracks: Dict[int, List[Tuple[int, float, float]]],
    width: int,
    height: int,
    fps: float,
    min_track_s: float = 1.0,
) -> Optional[Dict]:
    """
    Estimate the doorway line and entry direction from people trajectories
    ``track_id -> [(frame_idx, foot_x, foot_y), ...]``.

    A door is where people APPEAR and DISAPPEAR while walking in a consistent
    direction.  For every walking track we take its start (direction = where it goes next)
    and its end (direction reversed = where it came from).  Both point "into the room"
    at a real door, so they add up; at an occlusion hot-spot they cancel out, which is
    how the two are told apart.

    The line is drawn perpendicular to that flow, slightly INSIDE the room so that people
    are already visible on the outside before they cross it.
    """
    scale = float(max(width, height))
    ends = []
    for pts in tracks.values():
        if len(pts) < 3:
            continue
        if (pts[-1][0] - pts[0][0]) / fps < min_track_s:
            continue
        x0, y0, x1, y1 = pts[0][1], pts[0][2], pts[-1][1], pts[-1][2]
        disp = math.hypot(x1 - x0, y1 - y0)
        if disp < 0.12 * scale:
            continue
        ux, uy = (x1 - x0) / disp, (y1 - y0) / disp
        ends.append((x0, y0, ux, uy))
        ends.append((x1, y1, -ux, -uy))
    if len(ends) < 6:
        return None

    arr = np.array(ends, dtype=float)
    cell = 0.12 * scale
    gx = np.floor(arr[:, 0] / cell).astype(int)
    gy = np.floor(arr[:, 1] / cell).astype(int)
    best_mask, best_n = None, 0
    for cx_i, cy_i in set(zip(gx.tolist(), gy.tolist())):
        mask = (np.abs(gx - cx_i) <= 1) & (np.abs(gy - cy_i) <= 1)
        n = int(mask.sum())
        if n > best_n:
            best_n, best_mask = n, mask
    if best_mask is None or best_n < max(4, int(0.25 * len(arr))):
        return None

    cluster = arr[best_mask]
    centroid = cluster[:, :2].mean(axis=0)
    flow = cluster[:, 2:4].mean(axis=0)
    resultant = float(np.hypot(flow[0], flow[1]))
    if resultant < 0.5:
        return None
    flow = flow / resultant
    perp = np.array([-flow[1], flow[0]])

    rel = cluster[:, :2] - centroid
    along_std = float((rel @ flow).std())
    across_std = float((rel @ perp).std())
    offset = float(np.clip(0.05 * scale + along_std, 0.05 * scale, 0.15 * scale))
    half = float(np.clip(0.06 * scale + 2.0 * across_std, 0.10 * scale, 0.35 * scale))

    start, end = _clip_segment(centroid + flow * offset, perp, half, width, height)
    if math.hypot(end[0] - start[0], end[1] - start[1]) < 0.08 * scale:
        return None
    return {
        "line": (start, end),
        "entry_vector": (float(flow[0]), float(flow[1])),
        "confidence": resultant,
        "endpoints": len(arr),
    }


def scan_tracks(
    source: Path,
    model_path: str,
    tracker: str,
    fps: float,
    seconds: float,
    conf: float,
    imgsz: int,
) -> Dict[int, List[Tuple[int, float, float]]]:
    """Quick tracking pre-pass (own model + capture, ~12 fps) used to estimate the doorway."""
    cap = cv2.VideoCapture(str(source))
    scan_model = YOLO(model_path)
    stride = max(1, int(round(fps / 12.0)))
    max_frames = int(seconds * fps)
    tracks: Dict[int, List[Tuple[int, float, float]]] = {}
    frame_idx = 0
    try:
        while frame_idx < max_frames:
            ok, frame = cap.read()
            if not ok:
                break
            res = scan_model.track(
                frame, persist=True, tracker=tracker, classes=[PERSON], conf=conf, imgsz=imgsz, verbose=False
            )[0]
            if res.boxes is not None and res.boxes.id is not None:
                for b, tid in zip(res.boxes.xyxy.cpu().numpy(), res.boxes.id.int().cpu().tolist()):
                    tracks.setdefault(tid, []).append((frame_idx, float((b[0] + b[2]) / 2), float(b[3])))
            for _ in range(stride - 1):
                if not cap.grab():
                    break
            frame_idx += stride
    finally:
        cap.release()
    return tracks



# Track Analytics (time-based, resolution-independent)

@dataclass
class AnalyticsConfig:
    still_speed: float = 0.25       # body-heights / s: below = standing/sitting still
    moving_speed: float = 0.45      # body-heights / s: above = walking
    assign_s: float = 0.4           # time inside a zone before it is assigned
    switch_s: float = 1.0           # time inside ANOTHER zone before switching
    release_s: float = 1.5          # time outside every zone before releasing
    customer_dwell_s: float = 5.0   # seated time at one table that makes a CUSTOMER
    settled_s: float = 2.0          # still time at a table after which a person already counts toward table occupancy
    staff_observe_s: float = 6.0    # minimum time on screen before judging STAFF (was 20 s: a waiter passing for ~8 s was never labelled)
    staff_walk_ratio: float = 0.5   # share of time walking that marks STAFF
    staff_min_walk_s: float = 3.0   # absolute walking time required (a customer who just arrived and sat down has little)
    warmup_s: float = 3.0           # start-up phase: people already seated when the video starts are recognised faster
    initial_settled_s: float = 0.8  # settled_s used for people present at video start
    initial_dwell_s: float = 2.0    # customer_dwell_s used for people present at video start
    window_s: float = 1.5           # sliding window for speed
    reclaim_s: float = 4.0          # how long a vanished (walking) track may be re-adopted
    seated_reclaim_s: float = 90.0  # how long a vanished CUSTOMER may be re-adopted (occlusion by chair/table/people)
    seated_reclaim_scale: float = 1.0  # search radius for a seated customer, in body-heights


class TrackAnalytics:
    """
    Per-track behaviour over time.  States:
      - UNKNOWN : not enough evidence yet (new arrivals walking to their table stay here)
      - CUSTOMER: sat still at one table for >= customer_dwell_s (sticky, also while leaving)
      - STAFF   : on screen long enough, never settled at a table, and keeps walking
                  or moves between tables
    """

    def __init__(self, track_id: int, fps: float, cfg: AnalyticsConfig, uniform: Optional[UniformConfig] = None):
        self.track_id = track_id
        self.fps = fps
        self.cfg = cfg
        self.uniform = uniform
        self.uni_n = 0            # uniform observations
        self.uni_hits = 0         # observations whose torso matched the uniform colour
        self._uni_last_frame = -10**9
        self.positions: Deque[Tuple[int, float, float, float]] = deque(maxlen=max(5, int(cfg.window_s * fps)))
        self.total_frames = 0
        self.walk_frames = 0
        self.table_residency: Dict[int, int] = {}       # frames spent inside each zone
        self.seated_by_table: Dict[int, int] = {}       # frames inside a zone AND still
        self.assigned_table: Optional[int] = None
        self.pending_table: Optional[int] = None
        self.pending_count = 0
        self.frames_outside = 0
        self.state = "UNKNOWN"
        self.last_seen_frame = 0
        self.last_center: Tuple[float, float] = (0.0, 0.0)
        self.last_box: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
        self.first_frame = 0
        self.initial = False  # present during the start-up phase (already seated when the video began)

    def _settled_s(self) -> float:
        return self.cfg.initial_settled_s if self.initial else self.cfg.settled_s

    def _dwell_s(self) -> float:
        return self.cfg.initial_dwell_s if self.initial else self.cfg.customer_dwell_s

    # helpers 
    def _speed(self) -> float:
        """Net speed over the window in body-heights per second (perspective independent)."""
        if len(self.positions) < 5:
            return 0.0
        f0, x0, y0, _ = self.positions[0]
        f1, x1, y1, _ = self.positions[-1]
        dt = (f1 - f0) / self.fps
        if dt <= 0:
            return 0.0
        h = float(np.median([p[3] for p in self.positions]))
        return math.hypot(x1 - x0, y1 - y0) / max(h, 1.0) / dt

    def observe_uniform(self, frame: np.ndarray, box: Tuple[float, float, float, float], frame_idx: int) -> None:
        """Sample the torso colour (about 5 times per second) and update the uniform evidence."""
        if self.uniform is None or frame_idx - self._uni_last_frame < max(1, int(round(self.fps / 5.0))):
            return
        self._uni_last_frame = frame_idx
        ratio = uniform_match_ratio(frame, box, self.uniform)
        if ratio is None:
            return
        self.uni_n += 1
        if ratio >= self.uniform.pixel_ratio:
            self.uni_hits += 1

    def uniform_status(self) -> str:
        """'match' / 'no_match' / 'unknown' (no uniform configured or too little evidence)."""
        u = self.uniform
        if u is None or self.uni_n < u.min_samples:
            return "unknown"
        share = self.uni_hits / self.uni_n
        if share >= u.confirm_ratio:
            return "match"
        if share <= 0.2 and self.uni_n >= int(1.5 * u.min_samples):
            return "no_match"
        return "unknown"

    def is_settled(self) -> bool:
        """Sitting still inside an assigned table zone long enough to count as seated (before CUSTOMER)."""
        if self.assigned_table is None:
            return False
        if self._speed() >= self.cfg.moving_speed:  # walking away from the table: not seated any more
            return False
        return self.seated_by_table.get(self.assigned_table, 0) / self.fps >= self._settled_s()

    def adopt(self, old: "TrackAnalytics") -> None:
        """Take over the history of a track whose ID was lost (tracker ID switch)."""
        keep = self.track_id
        self.__dict__.update(copy.deepcopy(old.__dict__))
        self.track_id = keep

    # main update
    def update(self, frame_idx: int, box: Tuple[float, float, float, float], table_id: Optional[int]) -> None:
        x1, y1, x2, y2 = box
        cx, cy, h = (x1 + x2) / 2.0, (y1 + y2) / 2.0, max(1.0, y2 - y1)
        if self.total_frames == 0:  # very first observation of this person
            self.first_frame = frame_idx
            self.initial = frame_idx <= int(self.cfg.warmup_s * self.fps)
        self.last_seen_frame = frame_idx
        self.last_center = (cx, cy)
        self.last_box = (x1, y1, x2, y2)
        self.total_frames += 1
        self.positions.append((frame_idx, cx, cy, h))
        speed = self._speed()
        fps, cfg = self.fps, self.cfg

        # Zone assignment with hysteresis (all in seconds, so it is fps-independent)
        if table_id is None:
            self.frames_outside += 1
            self.pending_table, self.pending_count = None, 0
            if self.assigned_table is not None and self.frames_outside > max(3, int(cfg.release_s * fps)):
                self.assigned_table = None
        else:
            self.frames_outside = 0
            self.table_residency[table_id] = self.table_residency.get(table_id, 0) + 1
            if table_id == self.assigned_table:
                self.pending_table, self.pending_count = None, 0
            else:
                if table_id == self.pending_table:
                    self.pending_count += 1
                else:
                    self.pending_table, self.pending_count = table_id, 1
                need = cfg.assign_s if self.assigned_table is None else cfg.switch_s
                if self.pending_count >= max(2, int(need * fps)):
                    self.assigned_table = table_id
                    self.pending_table, self.pending_count = None, 0

        # Behaviour evidence
        if speed >= cfg.moving_speed:
            self.walk_frames += 1
        if table_id is not None and speed < cfg.still_speed:
            self.seated_by_table[table_id] = self.seated_by_table.get(table_id, 0) + 1

        self._classify()

    def _classify(self) -> None:
        cfg, fps = self.cfg, self.fps
        total_s = self.total_frames / fps
        dwell_s = self._dwell_s()
        seated_here_s = (
            self.seated_by_table.get(self.assigned_table, 0) / fps if self.assigned_table is not None else 0.0
        )
        max_seated_s = max(self.seated_by_table.values(), default=0) / fps
        uni = self.uniform_status()

        if uni == "match":
            self.state = "STAFF"   # the uniform is stronger evidence than behaviour (also when a waiter sits down)
        elif self.assigned_table is not None and seated_here_s >= dwell_s:
            self.state = "CUSTOMER"
        elif self.state == "CUSTOMER":
            pass  # customers stay customers while walking out
        elif total_s >= cfg.staff_observe_s and max_seated_s < dwell_s:
            # Someone who is sitting/standing still at a table right now is a customer-in-progress,
            # not staff (a new arrival walks for a few seconds first and then sits down).
            at_table_now = self.assigned_table is not None and self._speed() < cfg.moving_speed
            walk_s = self.walk_frames / fps
            walk_ratio = self.walk_frames / max(1, self.total_frames)
            visited = sum(1 for f in self.table_residency.values() if f >= 0.5 * fps)
            walks_a_lot = walk_ratio >= cfg.staff_walk_ratio or (visited >= 2 and walk_ratio >= 0.3)
            strict_block = uni == "no_match" and self.uniform is not None and self.uniform.strict
            if not at_table_now and walk_s >= cfg.staff_min_walk_s and walks_a_lot and not strict_block:
                self.state = "STAFF"


def _overlap_share(a, b) -> float:
    """Overlap area as a share of the SMALLER box (used to find table zones that overlap)."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return (ix * iy) / smaller if smaller > 0 else 0.0


def _box_iou(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = max(1.0, (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)
    return inter / union


def reclaim_lost_analytics(
    analytics: Dict[int, TrackAnalytics],
    new_ta: TrackAnalytics,
    box: Tuple[float, float, float, float],
    frame_idx: int,
    active_ids: set,
    fps: float,
    cfg: AnalyticsConfig,
    table_id: Optional[int] = None,
    door_zone=None,
) -> Optional[int]:
    """If a vanished track was last seen right where this new ID appeared, inherit its history."""
    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    h = max(1.0, box[3] - box[1])
    best_id, best_score = None, float("inf")
    for tid, old in analytics.items():
        if tid in active_ids or old is new_ta:
            continue
        gap = frame_idx - old.last_seen_frame
        seated_customer = old.state == "CUSTOMER" and old.assigned_table is not None
        # A seated customer who vanishes is almost always an occlusion / missed detection,
        # not someone who left -> remember them much longer and search a wider area.
        max_gap = (cfg.seated_reclaim_s if seated_customer else cfg.reclaim_s) * fps
        max_dist = (cfg.seated_reclaim_scale if seated_customer else 0.75) * h
        if gap < 1 or gap > max_gap:
            continue
        # A seated customer only comes back at HIS OWN table. Without this rule, new arrivals who walk in
        # near a customer that was briefly lost inherited that customer's history (and got hidden/mislabelled).
        if seated_customer and table_id != old.assigned_table:
            continue
        # People who vanish at the doorway are leaving; whoever appears there next is usually a NEW person.
        if not seated_customer and door_zone is not None and door_zone(old.last_center):
            continue
        dist = math.hypot(old.last_center[0] - cx, old.last_center[1] - cy)
        if dist <= max_dist and dist < best_score:
            best_id, best_score = tid, dist
    if best_id is not None:
        old = analytics[best_id]
        new_ta.adopt(old)
        # Keep a CUSTOMER's own record: the tracker often brings the old ID back a few frames later,
        # and it must not restart as "Person".  Other (walking) tracks are moved as before.
        if old.state != "CUSTOMER":
            analytics.pop(best_id)
        return best_id

    # Duplicate box: the detector/tracker sometimes puts a SECOND ID on a person who is still
    # tracked (the old ID is active, so the lookup above skips it).  A new ID whose box overlaps
    # an active CUSTOMER almost completely is the same person -> inherit instead of starting as "Person".
    for tid, old in analytics.items():
        if old is new_ta or tid not in active_ids or old.state != "CUSTOMER":
            continue
        if frame_idx - old.last_seen_frame > 1:
            continue
        if _box_iou(box, old.last_box) >= 0.45:
            new_ta.adopt(old)
            return tid
    return None



# Drawing helpers

def draw_badge(img, text: str, x: int, y: int, color, text_color=(0, 0, 0), scale: float = 0.42) -> None:
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    top = max(0, y - th - 6)
    cv2.rectangle(img, (x, top), (x + tw + 4, top + th + 6), color, -1)
    cv2.putText(img, text, (x + 2, top + th + 1), cv2.FONT_HERSHEY_SIMPLEX, scale, text_color, 1, cv2.LINE_AA)



# Main DineSight Pipeline

def build_quality_report(
    *,
    events: List[Dict],
    entries: int,
    exits: int,
    unique_ids: int,
    raw_ids: int,
    merged_ids: int,
    max_simultaneous: int,
    initial_occupancy: int,
    doorway_source: str,
    doorway_confidence: Optional[float],
    entry_source: str,
    tables_source: str,
    table_count: int,
    analytics_from_profile: List[str],
    analytics_from_cli: List[str],
    overlapping_table_pairs: int = 0,
    uniform_enabled: bool = False,
    uniform_staff_count: int = 0,
) -> Dict:
    """
    Self-assessment of a run: where the numbers come from and where to doubt them.
    Pure function (no video / model needed), so it can be unit-tested on its own.
    """
    # Net door events per detection source (a cancelled reversal removes the event it cancels).
    by_source: Dict[str, Dict[str, int]] = {}
    cancelled = 0
    for e in events:
        etype, src = e.get("type", ""), e.get("source", "line")
        bucket = by_source.setdefault(src, {"entries": 0, "exits": 0})
        if etype == "ENTRY":
            bucket["entries"] += 1
        elif etype == "EXIT":
            bucket["exits"] += 1
        elif etype == "CANCEL_ENTRY":
            bucket["entries"] -= 1
            cancelled += 1
        elif etype == "CANCEL_EXIT":
            bucket["exits"] -= 1
            cancelled += 1
    by_source.setdefault("line", {"entries": 0, "exits": 0})
    total_events = entries + exits
    guessed = sum(b["entries"] + b["exits"] for src, b in by_source.items() if src != "line")
    guessed_share = (guessed / total_events) if total_events > 0 else 0.0

    expected_people = initial_occupancy + entries
    ids_per_peak = round(unique_ids / max_simultaneous, 2) if max_simultaneous > 0 else None
    ids_vs_expected = round(unique_ids / expected_people, 2) if expected_people > 0 else None

    warnings: List[Dict[str, str]] = []

    def warn(level: str, code: str, message: str) -> None:
        warnings.append({"level": level, "code": code, "message": message})

    if doorway_source == "fallback":
        warn("critical", "door_fallback",
             "No doorway could be found, so a default centred line was used. Entry/exit counts are "
             "not reliable until the doorway line and entry direction are set (profile or --line/--entry-dir).")
    elif doorway_source == "auto":
        conf = f" (flow confidence {doorway_confidence:.2f})" if doorway_confidence is not None else ""
        warn("warning", "door_auto",
             f"The doorway line and entry direction were estimated automatically{conf}. Check them on the "
             "output video and save a profile for this camera.")
    if table_count == 0:
        warn("critical", "no_tables", "No tables were found, so table status (empty/occupied) is not available.")
    elif tables_source == "auto":
        warn("warning", "tables_auto",
             "Tables were discovered automatically from chair/table detections: some tables may be missing "
             "and capacity is guessed. Empty/occupied status is only as good as these zones; define the "
             "tables in a profile for reliable results.")
    if overlapping_table_pairs > 0:
        warn("warning", "tables_overlap",
             f"{overlapping_table_pairs} pair(s) of table zones overlap by more than 15% of the smaller zone, so a person can be assigned to "
             "the neighbouring table (wrong counts / false EMPTY). Redraw the tables so zones do not overlap.")
    if total_events >= 2 and guessed_share >= 0.25:
        warn("warning", "guessed_events",
             f"{guessed} of {total_events} door events ({guessed_share:.0%}) were inferred (person first seen "
             "inside the door zone, or lost at the door) rather than seen crossing the line. "
             "Expect some over- or under-counting.")
    if total_events == 0 and unique_ids >= 5:
        warn("warning", "no_door_events",
             f"{unique_ids} people were tracked but nothing was counted at the door. The line may be "
             "misplaced, or nobody used this door in the clip.")
    # Staff and people who never use this door legitimately add some IDs, so only flag a clear excess.
    if ids_vs_expected is not None and ids_vs_expected >= 2.0 and unique_ids - expected_people >= 5:
        warn("warning", "id_fragmentation",
             f"{unique_ids} track IDs for about {expected_people} people expected (people inside at the start "
             f"+ entries). The tracker probably split people into several IDs, so ID-based numbers "
             "(customers/staff/unique IDs) are inflated.")
    if uniform_enabled:
        if uniform_staff_count == 0 and unique_ids >= 5:
            warn("warning", "uniform_no_match",
                 "A staff uniform colour is configured but nobody matched it. Re-sample the colour on a clear "
                 "frame (lighting differs between cameras), or check that staff are visible.")
        else:
            warn("info", "staff_uniform",
                 f"Staff identified mainly by uniform colour ({uniform_staff_count} matched); behaviour is a fallback. "
                 "Customers wearing a similar colour (especially black/grey) can be mislabelled as staff.")
    else:
        warn("info", "staff_behaviour_only",
             "Staff vs customer is inferred from behaviour (walking vs sitting) only; it can be wrong in venues "
             "where staff also sit or customers keep moving. Sampling the uniform colour in the profile is stronger.")

    n_critical = sum(1 for w in warnings if w["level"] == "critical")
    n_warning = sum(1 for w in warnings if w["level"] == "warning")
    confidence = "low" if (n_critical or n_warning >= 3) else ("medium" if n_warning else "high")

    return {
        "confidence": confidence,
        "ids": {
            "tracker_ids_raw": raw_ids,
            "ids_merged_into_earlier_id": merged_ids,
            "unique_track_ids": unique_ids,
            "max_simultaneous_people": max_simultaneous,
            "ids_per_peak_person": ids_per_peak,
            "ids_vs_expected_people": ids_vs_expected,
        },
        "door_events": {
            "by_source": by_source,
            "cancelled_reversals": cancelled,
            "guessed": guessed,
            "guessed_share": round(guessed_share, 3),
        },
        "sources": {
            "doorway": doorway_source,
            "doorway_confidence": doorway_confidence,
            "entry": entry_source,
            "tables": tables_source,
            "analytics_from_profile": analytics_from_profile,
            "analytics_from_cli": analytics_from_cli,
        },
        "warnings": warnings,
    }


def _normalize_source(source: Source) -> Tuple[Union[str, int], bool]:
    """Return (value for cv2.VideoCapture, is_live)."""
    if isinstance(source, int):
        return source, True
    text = str(source)
    if text.isdigit():
        return int(text), True
    if text.lower().startswith(("rtsp://", "rtmp://", "http://", "https://")):
        return text, True
    return text, False


def run_dinesight_pipeline(
    video_path: Source,
    output_path: Path,
    profile_name: Optional[str] = None,
    line_override: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None,
    entry_dir_override: Optional[str] = None,
    model_path: str = MODEL_PATH,
    conf: float = 0.10,
    imgsz: int = 640,
    auto_door: bool = True,
    scan_seconds: float = 30.0,
    write_video: bool = True,
    report_path: Optional[Path] = None,
    cfg: Optional[AnalyticsConfig] = None,
    cfg_overrides: Optional[Dict[str, float]] = None,
):
    cap_source, is_live = _normalize_source(video_path)
    if not is_live and not Path(cap_source).exists():
        raise FileNotFoundError(f"Video file not found: {Path(cap_source).resolve()}")

    cap = cv2.VideoCapture(cap_source)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video source: {cap_source}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or not math.isfinite(fps) or fps < 1:
        fps = 25.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    scale = max(width, height)
    name_for_profile = Path("live") if is_live else Path(cap_source)

    # Classification settings: defaults < profile "analytics" block < command line.
    # (An explicit `cfg` object, e.g. from code calling this function, is used as is.)
    cfg_from_profile: Dict[str, float] = {}
    cfg_from_cli: Dict[str, float] = {}
    if cfg is None:
        cfg = AnalyticsConfig()
        cfg_from_profile = load_profile_analytics(profile_name, name_for_profile)
        cfg_from_cli = {k: float(v) for k, v in (cfg_overrides or {}).items() if v is not None}
        for key, value in {**cfg_from_profile, **cfg_from_cli}.items():
            setattr(cfg, key, value)
    uniform_cfg = load_profile_uniform(profile_name, name_for_profile)
    if uniform_cfg is not None:
        print(f"[Config] Staff uniform colour: {len(uniform_cfg.samples)} sample(s)"
              f"{' (strict)' if uniform_cfg.strict else ''}")

    print("=" * 70)
    print("DineSight: Unified Vision & Counting Analytics")
    print(f"Input         : {cap_source if is_live else Path(cap_source).resolve()}")
    print(f"Resolution    : {width}x{height} @ {fps:.2f} fps ({total_frames if total_frames > 0 else 'live'} frames)")
    if write_video:
        print(f"Output Video  : {output_path.resolve()}")

    tracker_cfg = resolve_tracker_config()
    # The tracking model is used ONLY for tracking. Discovery / scanning use their own
    # instance so predict() calls cannot interfere with the tracker's state.
    model = YOLO(model_path)
    aux_model = YOLO(model_path)

    # 1. Scene profile, or dynamic discovery
    doorway_line, entry_direction, tables = load_scene_profile(profile_name, name_for_profile, width, height)
    entry_vector: Optional[Tuple[float, float]] = None
    doorway_source = "profile" if doorway_line is not None else "auto"
    doorway_confidence: Optional[float] = None
    entry_source = "profile" if doorway_line is not None else "auto"
    tables_source = "profile" if tables else "auto"

    if line_override:
        doorway_line = (
            (int(line_override[0][0] * width), int(line_override[0][1] * height)),
            (int(line_override[1][0] * width), int(line_override[1][1] * height)),
        )
        doorway_source = "manual"
        entry_source = "manual"
    elif doorway_line is None:
        estimate = None
        if auto_door and not is_live:
            print(f"[Setup] No doorway configured. Scanning the first {scan_seconds:.0f}s to locate the door...")
            tracks = scan_tracks(Path(cap_source), model_path, tracker_cfg, fps, scan_seconds, conf, imgsz)
            estimate = estimate_doorway(tracks, width, height, fps)
        if estimate:
            doorway_line = estimate["line"]
            entry_vector = estimate["entry_vector"]
            doorway_source = "auto"
            doorway_confidence = float(estimate["confidence"])
            entry_source = "auto"
            print(
                f"[Setup] Auto doorway from {estimate['endpoints']} track endpoints "
                f"(flow confidence {estimate['confidence']:.2f}). Check the overlay, and pass "
                f"--line / --entry-dir if it is wrong."
            )
        else:
            doorway_line = (
                (int(0.50 * width), int(0.10 * height)),
                (int(0.50 * width), int(0.90 * height)),
            )
            doorway_source = "fallback"
            entry_source = "default"
            print("[Setup Warning] Could not estimate a doorway: using a centred vertical line. "
                  "Pass --line and --entry-dir for reliable counts.")

    if entry_dir_override:
        entry_direction, entry_vector = entry_dir_override, None
        entry_source = "manual"

    if not tables:
        print("[Setup] No pre-defined tables. Running dynamic table auto-discovery...")
        tables = discover_tables_dynamically(sample_video_frames(cap), aux_model, width, height)
        tables_source = "auto"
        print(f"[Setup] Discovered {len(tables)} dynamic table zones.")
    else:
        tables_source = "profile"
        print(f"[Setup] Active Table Zones: {len(tables)}")

    shown_dir = f"vector=({entry_vector[0]:+.2f},{entry_vector[1]:+.2f})" if entry_vector else entry_direction
    print(f"[Setup] Counting Line: {doorway_line[0]} -> {doorway_line[1]} (entry: {shown_dir})")
    print("=" * 70)

    tolerance_px = max(18, int(width * 0.025))
    counter = DineSightCounter(
        line_start=doorway_line[0],
        line_end=doorway_line[1],
        entry_direction=entry_direction,
        tolerance_px=tolerance_px,
        cooldown_frames=max(6, int(0.5 * fps)),
        stale_after_frames=max(30, int(3 * fps)),
        tables=tables,
        dead_band_px=max(6, int(tolerance_px * 0.5)),
        entry_vector=entry_vector,
        reid_window_frames=max(8, int(2 * fps)),
        reid_distance_px=max(30.0, 0.06 * scale),
        table_hold_frames=max(3, int(0.8 * fps)),
        # People entering through glass doors are often detected only after they passed the line.
        spawn_zone_px=(0.12 * scale) if SPAWN_ENTRY else 0.0,
        spawn_min_travel_px=max(20.0, 0.03 * scale),
        spawn_window_frames=max(24, int(3 * fps)),
        start_grace_frames=int(1.0 * fps),
    )

    writer = None
    if write_video:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(str(output_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
        if not writer.isOpened():
            cap.release()
            raise RuntimeError(f"Could not create output video: {output_path.resolve()}")

    track_analytics: Dict[int, TrackAnalytics] = {}
    flashes: List[Tuple[str, Tuple[int, int, int], Tuple[int, int], int]] = []
    table_summary = counter.evaluate_tables({})
    customers_estimated = staff_estimated = unknown_people = 0
    frame_idx = 0

    # Run-level bookkeeping for the JSON report / dashboard.
    seen_track_ids: set = set()
    raw_track_ids: set = set()      # IDs exactly as the tracker issued them (before merging)
    max_simultaneous = 0            # most distinct people visible in one frame
    alias: Dict[int, int] = {}  # tracker ID -> first ID of the same person (ID switches)
    canon = lambda t: alias.get(t, t)
    customer_track_ids: set = set()
    staff_track_ids: set = set()
    uniform_staff_ids: set = set()   # staff confirmed by uniform colour (never demoted to customer)
    active_track_ids: set = set()
    table_peak: Dict[int, int] = {t["id"]: 0 for t in tables}
    table_visitors: Dict[int, set] = {t["id"]: set() for t in tables}
    occupancy_history: List[Dict] = []
    warmup_frames = max(1, int(cfg.warmup_s * fps))
    next_sample_frame = warmup_frames  # no occupancy samples while the system is still calibrating
    initial_occupancy: Optional[int] = None   # people already inside when the video started
    entry_side = counter.doorway.entry_side
    in_door_zone = lambda p: distance_to_segment(p, doorway_line[0], doorway_line[1]) <= 0.12 * scale

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break

            result = model.track(
                frame,
                persist=True,
                tracker=tracker_cfg,
                classes=[PERSON],
                conf=conf,
                imgsz=imgsz,
                verbose=False,
            )[0]
            annotated = frame.copy()

            boxes, ids = [], []
            if result.boxes is not None and result.boxes.id is not None:
                boxes = result.boxes.xyxy.cpu().numpy()
                ids = result.boxes.id.int().cpu().tolist()

            active_track_ids = set(ids)
            raw_track_ids.update(ids)
            table_occupants: Dict[int, List[int]] = {t["id"]: [] for t in tables}
            customers_estimated = staff_estimated = unknown_people = 0
            frame_cids: Dict[int, Tuple[float, float, float, float]] = {}
            warming = frame_idx < warmup_frames

            # Periodic cleanup of stale tracks
            if frame_idx % 60 == 0:
                counter.cleanup_stale_tracks(frame_idx)
                stale_ids = [
                    tid for tid, ta in track_analytics.items()
                    if frame_idx - ta.last_seen_frame > max(
                        120, int(((cfg.seated_reclaim_s if ta.state == "CUSTOMER" else cfg.reclaim_s) + 2) * fps)
                    )
                    and tid not in active_track_ids
                ]
                for tid in stale_ids:
                    del track_analytics[tid]

            for box, tid in zip(boxes, ids):
                x1, y1, x2, y2 = [int(v) for v in box]
                cx = (x1 + x2) // 2
                foot = (cx, y2)

                # 1. Doorway crossing (count.py)
                event = counter.update_doorway(tid, foot, frame_idx, body_height=float(y2 - y1))
                if event:
                    kind = event[0]
                    flashes.append((kind, (0, 255, 0) if kind == "ENTRY" else (0, 0, 255), foot, frame_idx + int(fps)))

                # 2. Table zone for this person
                candidate_table_id = pick_table_zone((x1, y1, x2, y2), tables)

                # 3. Behaviour analytics + classification
                ta = track_analytics.get(tid)
                if ta is None:
                    ta = TrackAnalytics(tid, fps, cfg, uniform_cfg)
                    adopted = reclaim_lost_analytics(
                        track_analytics, ta, (x1, y1, x2, y2), frame_idx, active_track_ids, fps, cfg,
                        table_id=candidate_table_id, door_zone=in_door_zone,
                    )
                    if adopted is not None:
                        alias[tid] = canon(adopted)
                    track_analytics[tid] = ta
                    seen_track_ids.add(canon(tid))
                if uniform_cfg is not None:
                    ta.observe_uniform(frame, (x1, y1, x2, y2), frame_idx)
                ta.update(frame_idx, (x1, y1, x2, y2), candidate_table_id)

                # Stable display ID: after a tracker ID switch the person keeps the ID they had before.
                cid = canon(tid)
                prev_box = frame_cids.get(cid)
                if prev_box is not None:
                    if _box_iou((x1, y1, x2, y2), prev_box) >= 0.45:
                        continue  # tracker put a second ID on the same person: draw/count him once
                    cid = tid     # a different person that wrongly inherited this ID: keep the own ID
                frame_cids[cid] = (x1, y1, x2, y2)
                seen_track_ids.add(cid)  # every ID that is shown/counted must also be in the "seen" set

                if ta.state == "CUSTOMER":
                    customer_track_ids.add(cid)
                elif ta.state == "STAFF":
                    staff_track_ids.add(cid)
                    if ta.uniform_status() == "match":
                        uniform_staff_ids.add(cid)

                if ta.state == "CUSTOMER":
                    customers_estimated += 1
                    if ta.assigned_table is not None:
                        table_occupants[ta.assigned_table].append(cid)
                        role_label = f"Customer #{cid} (T{ta.assigned_table})"
                    else:
                        role_label = f"Customer #{cid}"
                    box_color = (255, 180, 0)
                elif ta.state == "STAFF":
                    staff_estimated += 1
                    role_label = f"Staff #{cid}"
                    box_color = (0, 215, 255)
                else:
                    unknown_people += 1
                    role_label = f"Person #{cid}"
                    if ta.is_settled():  # sitting at a table already: counts for occupancy, label stays neutral
                        table_occupants[ta.assigned_table].append(cid)
                        role_label = f"Seated #{cid} (T{ta.assigned_table})"
                    box_color = (180, 180, 180)

                cv2.rectangle(annotated, (x1, y1), (x2, y2), box_color, 2)
                draw_badge(annotated, role_label, x1, y1, box_color)

            max_simultaneous = max(max_simultaneous, len(frame_cids))

            # 3b. IDs that vanished walking out through the door
            for kind, _total, pos in counter.check_doorway_lost(frame_idx):
                flashes.append((kind, (0, 0, 255), (int(pos[0]), int(pos[1])), frame_idx + int(fps)))

            # 3c. People already inside at the start: the doorway only sees people who cross it, so
            #     "inside now" = people inside when calibration ended - net change up to then + net change since.
            if initial_occupancy is None and frame_idx >= warmup_frames - 1:
                inside_now = 0
                for box, tid in zip(boxes, ids):
                    foot_pt = (int((box[0] + box[2]) / 2), int(box[3]))
                    if signed_side(foot_pt, doorway_line[0], doorway_line[1]) == entry_side:
                        inside_now += 1
                initial_occupancy = max(0, inside_now - counter.get_summary()["net_change"])

            # 4. Table statuses
            table_summary = counter.evaluate_tables(table_occupants, frame_idx)
            empty_tables = table_summary["empty_tables"]
            occupied_tables = table_summary["occupied_tables"]
            full_tables = table_summary["full_tables"]

            for t_id, t_info in table_summary["table_statuses"].items():
                table_peak[t_id] = max(table_peak.get(t_id, 0), t_info["count"])
                if t_info["seated_ids"]:
                    table_visitors.setdefault(t_id, set()).update(canon(i) for i in t_info["seated_ids"])

            if frame_idx >= next_sample_frame:
                occupancy_history.append({
                    "frame": frame_idx,
                    "time_s": round(frame_idx / fps, 2),
                    "seated": sum(t["count"] for t in table_summary["table_statuses"].values()),
                    "occupied_tables": len(occupied_tables) + len(full_tables),
                    "empty_tables": len(empty_tables),
                    "tables": {
                        t_id: info["count"]
                        for t_id, info in table_summary["table_statuses"].items()
                    },
                })
                next_sample_frame = frame_idx + max(1, int(round(fps)))

            for t in tables:
                bx1, by1, bx2, by2 = t["box"]
                t_info = table_summary["table_statuses"][t["id"]]
                count_val, cap_val, status_str = t_info["count"], t_info["capacity"], t_info["status"]
                if warming:
                    t_color, badge = (160, 160, 160), f"{t['name']}: CALIBRATING"
                elif status_str == "EMPTY":
                    t_color, badge = (0, 255, 0), f"{t['name']}: EMPTY (0/{cap_val})"
                elif status_str == "FULL":
                    t_color, badge = (0, 0, 255), f"{t['name']}: FULL ({count_val}/{cap_val})"
                else:
                    t_color, badge = (0, 165, 255), f"{t['name']}: OCCUPIED ({count_val}/{cap_val})"
                cv2.rectangle(annotated, (bx1, by1), (bx2, by2), t_color, 2)
                draw_badge(annotated, badge, bx1, by1 + 22, (20, 20, 20), t_color, 0.40)

            # Doorway line + entry arrow
            cv2.line(annotated, doorway_line[0], doorway_line[1], (255, 255, 0), 3)
            cv2.circle(annotated, doorway_line[0], 6, (0, 255, 255), -1)
            cv2.circle(annotated, doorway_line[1], 6, (0, 255, 255), -1)
            line_mid = (
                (doorway_line[0][0] + doorway_line[1][0]) // 2,
                (doorway_line[0][1] + doorway_line[1][1]) // 2,
            )
            vec = entry_vector or {
                "left_to_right": (1.0, 0.0), "right_to_left": (-1.0, 0.0),
                "top_to_bottom": (0.0, 1.0), "bottom_to_top": (0.0, -1.0),
            }.get(entry_direction, (1.0, 0.0))
            vn = math.hypot(vec[0], vec[1]) or 1.0
            arrow_end = (int(line_mid[0] + vec[0] / vn * 45), int(line_mid[1] + vec[1] / vn * 45))
            cv2.arrowedLine(annotated, line_mid, arrow_end, (0, 255, 0), 2, tipLength=0.35)
            cv2.putText(annotated, "DOORWAY (arrow = ENTRY)", (line_mid[0] + 6, line_mid[1] - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 0), 1, cv2.LINE_AA)

            flashes = [f for f in flashes if f[3] >= frame_idx]
            for text, color, pos, _ in flashes:
                cv2.putText(annotated, f"{text} +1", (pos[0] + 8, max(20, pos[1] - 30)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2, cv2.LINE_AA)

            # 5. HUD
            doorway_stats = counter.get_summary()
            entries, exits, net_change = doorway_stats["entries"], doorway_stats["exits"], doorway_stats["net_change"]

            ui = min(2.0, max(0.8, width / 960.0))  # HUD scales with the video resolution
            fs, line_h = 0.52 * ui, int(26 * ui)
            if warming:
                inside_txt = "Inside Now    : calibrating..."
                empty_list_str = "Empty Tables  : calibrating..."
                tables_txt = f"Tables Total  : {len(tables)} (calibrating...)"
            else:
                inside_now_est = max(0, (initial_occupancy or 0) + net_change)
                inside_txt = f"Inside Now    : {inside_now_est} est. (started with {initial_occupancy or 0})"
                tables_txt = f"Tables Total  : {len(tables)} (Active: {len(occupied_tables) + len(full_tables)}, Empty: {len(empty_tables)})"
                empty_list_str = f"Empty Tables  : {', '.join(empty_tables[:3])}" if empty_tables else "Empty Tables  : None"
            hud_lines = [
                (f"Doorway Flow  : Entries={entries} | Exits={exits} (Net Change: {net_change:+d})", (0, 255, 255)),
                (inside_txt, (255, 255, 255)),
                (f"Detected Now  : {len(boxes)} boxes ({len(active_track_ids)} active tracks)", (255, 255, 255)),
                (f"Classification: Customers={customers_estimated} | Staff={staff_estimated} | Unknown={unknown_people}", (255, 215, 0)),
                (tables_txt, (0, 165, 255)),
                (empty_list_str, (0, 255, 0)),
            ]
            title = "DINESIGHT VISION ANALYTICS"
            text_w = max(cv2.getTextSize(t, cv2.FONT_HERSHEY_SIMPLEX, fs, 1)[0][0] for t, _ in hud_lines)
            hud_w = min(width - 24, max(int(text_w + 24), int(300 * ui)))
            hud_h = int(line_h * (len(hud_lines) + 1) + 18 * ui)
            overlay = annotated.copy()
            cv2.rectangle(overlay, (12, 12), (12 + hud_w, 12 + hud_h), (18, 18, 18), -1)
            cv2.addWeighted(overlay, 0.85, annotated, 0.15, 0, annotated)
            cv2.rectangle(annotated, (12, 12), (12 + hud_w, 12 + hud_h), (100, 100, 100), 1)
            y = 12 + line_h
            cv2.putText(annotated, title, (20, y), cv2.FONT_HERSHEY_SIMPLEX, fs * 1.1, (255, 255, 255), 2, cv2.LINE_AA)
            for text, color in hud_lines:
                y += line_h
                cv2.putText(annotated, text, (20, y), cv2.FONT_HERSHEY_SIMPLEX, fs, color, 1, cv2.LINE_AA)

            if writer is not None:
                writer.write(annotated)
            frame_idx += 1

            if frame_idx % 75 == 0 or frame_idx == total_frames:
                print(
                    f"Frame {frame_idx:4d}/{total_frames if total_frames > 0 else '?'}: "
                    f"Entries={entries}, Exits={exits}, Net={net_change:+d} | "
                    f"Cust={customers_estimated}, Staff={staff_estimated}, Unk={unknown_people} | "
                    f"Empty Tables={len(empty_tables)}"
                )
    except KeyboardInterrupt:
        print("\n[Interrupted] Stopping and writing the summary...")
    finally:
        cap.release()
        if writer is not None:
            writer.release()

    # A person who was first judged STAFF and later sat down long enough is a CUSTOMER: never count them twice,
    # so that customers + staff + unknown always add up to the unique IDs.
    customer_track_ids -= uniform_staff_ids
    staff_track_ids |= uniform_staff_ids
    staff_track_ids -= customer_track_ids
    final_stats = counter.get_summary()
    empty_tables = table_summary["empty_tables"]
    occupied_total = len(table_summary["occupied_tables"]) + len(table_summary["full_tables"])
    print("\n" + "=" * 70)
    print("DineSight Analysis Complete")
    print(f"Total Processed Frames : {frame_idx}")
    print(f"Doorway Entries        : {final_stats['entries']}")
    print(f"Doorway Exits          : {final_stats['exits']}")
    print(f"Net Change             : {final_stats['net_change']:+d}")
    print(f"Inside at start / end  : {initial_occupancy or 0} / {max(0, (initial_occupancy or 0) + final_stats['net_change'])} (estimates)")
    print(f"Total Tables Monitored : {len(tables)}")
    print(f"Empty Tables Count     : {len(empty_tables)} -> {empty_tables}")
    print(f"Occupied Tables Count  : {occupied_total}")
    print(f"Unique Track IDs       : {len(seen_track_ids)}")
    print(f"Customers / Staff      : {len(customer_track_ids)} / {len(staff_track_ids)}")
    print(f"Unknown / Visible Now  : {len(seen_track_ids - customer_track_ids - staff_track_ids)} / {len(active_track_ids)}")
    quality = build_quality_report(
        events=counter.get_events(),
        entries=final_stats["entries"],
        exits=final_stats["exits"],
        unique_ids=len(seen_track_ids),
        raw_ids=len(raw_track_ids),
        merged_ids=len(alias),
        max_simultaneous=max_simultaneous,
        initial_occupancy=initial_occupancy or 0,
        doorway_source=doorway_source,
        doorway_confidence=doorway_confidence,
        entry_source=entry_source,
        tables_source=tables_source,
        table_count=len(tables),
        analytics_from_profile=sorted(cfg_from_profile),
        analytics_from_cli=sorted(cfg_from_cli),
        overlapping_table_pairs=sum(
            1 for i in range(len(tables)) for j in range(i + 1, len(tables))
            if _overlap_share(tables[i]["box"], tables[j]["box"]) > 0.15
        ),
        uniform_enabled=uniform_cfg is not None,
        uniform_staff_count=len(uniform_staff_ids),
    )
    print(f"Result confidence      : {quality['confidence'].upper()} "
          f"(peak {max_simultaneous} people on screen, {len(seen_track_ids)} unique IDs)")
    for w in quality["warnings"]:
        if w["level"] != "info":
            print(f"  [{w['level'].upper()}] {w['message']}")
    if write_video:
        print(f"Output Video Saved     : {output_path.resolve()}")
    print("=" * 70)

    if report_path is not None:
        table_report = {}
        for t_id, info in table_summary["table_statuses"].items():
            table_report[str(t_id)] = {
                **info,
                "peak_count": table_peak.get(t_id, 0),
                "unique_seated_ids": sorted(table_visitors.get(t_id, set())),
            }
        line_normalized = [
            [round(doorway_line[0][0] / width, 4), round(doorway_line[0][1] / height, 4)],
            [round(doorway_line[1][0] / width, 4), round(doorway_line[1][1] / height, 4)],
        ]
        report = {
            "frames": frame_idx,
            "fps": round(fps, 3),
            "duration_s": round(frame_idx / fps, 2) if fps else 0,
            "resolution": {"width": width, "height": height},
            "source": str(video_path),
            "entries": final_stats["entries"],
            "exits": final_stats["exits"],
            "net_change": final_stats["net_change"],
            "initial_occupancy_estimate": initial_occupancy or 0,
            "inside_now_estimate": max(0, (initial_occupancy or 0) + final_stats["net_change"]),
            "doorway_line": [list(doorway_line[0]), list(doorway_line[1])],
            "doorway_line_normalized": line_normalized,
            "entry": list(entry_vector) if entry_vector else entry_direction,
            "scene": {
                "profile": profile_name or "automatic",
                "doorway_source": doorway_source,
                "doorway_confidence": doorway_confidence,
                "entry_source": entry_source,
                "tables_source": tables_source,
                "table_count": len(tables),
            },
            "classification": {
                "unique_track_ids": len(seen_track_ids),
                "customers": len(customer_track_ids),
                "staff": len(staff_track_ids),
                "unknown": len(seen_track_ids - customer_track_ids - staff_track_ids),
                "visible_at_end": len(active_track_ids),
            },
            "table_summary": {
                "total_tables": len(tables),
                "empty": len(empty_tables),
                "occupied": len(occupied_tables),
                "full": len(full_tables),
                "total_seated": sum(t["count"] for t in table_summary["table_statuses"].values()),
                "empty_tables": empty_tables,
                "occupied_tables": occupied_tables,
                "full_tables": full_tables,
            },
            "tables": table_report,
            "quality": quality,
            "staff_uniform": {
                "enabled": uniform_cfg is not None,
                "samples": [list(x) for x in uniform_cfg.samples] if uniform_cfg else [],
                "matched_staff_ids": sorted(uniform_staff_ids),
            },
            "analytics_settings": {f.name: getattr(cfg, f.name) for f in dataclass_fields(AnalyticsConfig)},
            "occupancy_history": occupancy_history,
            "events": [dict(e, time_s=round(e["frame"] / fps, 2)) for e in counter.get_events()],
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"Report saved           : {report_path.resolve()}")


# =====================================================================
# CLI Entry Point
# =====================================================================
def main():
    parser = argparse.ArgumentParser(description="DineSight Unified Vision Analytics")
    parser.add_argument("video_path", nargs="?", default=str(DEFAULT_VIDEO),
                        help="Input video file, webcam index (0), or rtsp:// URL")
    parser.add_argument("output_path", nargs="?", default=str(DEFAULT_OUTPUT), help="Output video file path")
    parser.add_argument("--profile", default=None, help="Scene profile name or JSON path (e.g. 'case', 'test2', 'test')")
    parser.add_argument("--line", default=None, help="Doorway line normalized 'x1,y1,x2,y2' (0.0 to 1.0)")
    parser.add_argument("--entry-dir", default=None, choices=VALID_DIRECTIONS, help="Direction of travel that counts as ENTRY")
    parser.add_argument("--model", default=MODEL_PATH, help="YOLO weights (yolo11s.pt / yolo11m.pt are more accurate)")
    parser.add_argument("--conf", type=float, default=0.10, help="Detector confidence (ByteTrack needs low-score boxes)")
    parser.add_argument("--imgsz", type=int, default=640, help="Inference size (960 or 1280 helps small/far people)")
    parser.add_argument("--no-auto-door", action="store_true", help="Do not estimate the doorway from trajectories")
    parser.add_argument("--scan-seconds", type=float, default=30.0, help="How much video to scan for the doorway")
    parser.add_argument("--no-video", action="store_true", help="Skip writing the annotated video (faster)")
    parser.add_argument("--report", default=None, help="Write a JSON report (counts, tables, event log) to this path")
    parser.add_argument("--customer-dwell", type=float, default=None, help="Seconds seated at a table to count as a customer")
    parser.add_argument("--no-spawn-entry", action="store_true",
                        help="Count doorway crossings only; disable the 'first seen inside the door zone' entry/exit logic")
    parser.add_argument("--staff-observe", type=float, default=None, help="Seconds on screen before a walker can be labelled staff")
    parser.add_argument("--staff-min-walk", type=float, default=None, help="Seconds of walking required before a person can be labelled staff")
    parser.add_argument("--warmup-seconds", type=float, default=None, help="Start-up calibration time before table status is shown")
    args = parser.parse_args()

    line_override = None
    if args.line:
        coords = [float(v) for v in args.line.split(",")]
        if len(coords) != 4:
            parser.error("--line needs 4 comma-separated numbers: x1,y1,x2,y2")
        line_override = ((coords[0], coords[1]), (coords[2], coords[3]))

    global SPAWN_ENTRY
    SPAWN_ENTRY = not args.no_spawn_entry

    cfg_overrides = {}
    if args.customer_dwell is not None:
        cfg_overrides["customer_dwell_s"] = args.customer_dwell
    if args.staff_observe is not None:
        cfg_overrides["staff_observe_s"] = args.staff_observe
    if args.staff_min_walk is not None:
        cfg_overrides["staff_min_walk_s"] = args.staff_min_walk
    if args.warmup_seconds is not None:
        cfg_overrides["warmup_s"] = args.warmup_seconds

    run_dinesight_pipeline(
        video_path=args.video_path,
        output_path=Path(args.output_path),
        profile_name=args.profile,
        line_override=line_override,
        entry_dir_override=args.entry_dir,
        model_path=args.model,
        conf=args.conf,
        imgsz=args.imgsz,
        auto_door=not args.no_auto_door,
        scan_seconds=args.scan_seconds,
        write_video=not args.no_video,
        report_path=Path(args.report) if args.report else None,
        cfg_overrides=cfg_overrides,
    )


if __name__ == "__main__":
    main()