"""
DineSight Detection Module (src/detect.py)

Architecture:
1. Video Ingestion & Validation (codec, resolution, fps)
2. Detection & Multi-Object Tracking (YOLOv11 + ByteTrack)
3. Scene Configuration:
   - Explicit Profile Mode (loads JSON config from profiles/ or file path)
   - Dynamic Auto-Discovery Mode (clusters tables/chairs from video frames)
4. Per-Track Lifecycle, Movement & Role Classification:
   - Recent velocity / sliding window displacement (normalized for FPS)
   - 4-state lifecycle: UNKNOWN, CUSTOMER, STAFF, LOST
   - Stable table assignment (hysteresis / temporal smoothing)
   - Tracks assigned to at most one table at a time
5. Counting & Metrics Orchestration (delegated to DineSightCounter):
   - Explicit separated metrics: people_detected_now, active_tracks,
     customers_estimated, staff_estimated, unknown_people,
     entries, exits, net_change
6. Visualization & Export (Live HUD, table polygons/boxes, role tags)
"""

from __future__ import annotations

import argparse
from collections import deque
import json
from pathlib import Path
import sys
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from ultralytics import YOLO

# Ensure src can import count
CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

try:
    from count import DineSightCounter
except ImportError:
    from src.count import DineSightCounter

# Configuration Defaults
DEFAULT_VIDEO = Path("videos/case.mp4")
DEFAULT_OUTPUT = Path("videos/detected_case.mp4")
MODEL_PATH = "yolo11n.pt"
TRACKER_CONFIG = "bytetrack_custom.yaml"
PROFILES_DIR = Path("profiles")


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
    2. Fallback to check if a profile matches a known profile name.
    3. Return None to trigger dynamic auto-discovery.
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

    # Optional fallback based on known video name (convenience for benchmark test runs)
    if config_data is None:
        video_stem = video_path.stem.lower()
        candidate = PROFILES_DIR / f"{video_stem}.json"
        if candidate.exists():
            with open(candidate, "r", encoding="utf-8") as f:
                config_data = json.load(f)
            print(f"[Config] Automatically resolved matching profile: {candidate.name}")

    if not config_data:
        return None, "left_to_right", []

    # Parse doorway line
    line = None
    if "line_start" in config_data and "line_end" in config_data:
        ls = config_data["line_start"]
        le = config_data["line_end"]
        line = (
            (int(ls[0] * width), int(ls[1] * height)),
            (int(le[0] * width), int(le[1] * height)),
        )

    entry_direction = config_data.get("entry_direction", "left_to_right")

    # Parse tables
    tables = []
    for t in config_data.get("tables", []):
        bx = t["box_ratio"]
        tables.append({
            "id": t["id"],
            "name": t.get("name", f"Table {t['id']}"),
            "box": (
                int(bx[0] * width),
                int(bx[1] * height),
                int(bx[2] * width),
                int(bx[3] * height),
            ),
            "capacity": t.get("capacity", 4),
        })

    return line, entry_direction, tables


# Dynamic Table Auto-Discovery
def discover_tables_dynamically(
    cap: cv2.VideoCapture,
    model: YOLO,
    width: int,
    height: int,
    sample_frames: int = 35,
) -> List[Dict]:
    """
    Scans initial sample frames of any video to discover stable dining tables
    and seating clusters using YOLO dining table (60) and chair (56) detections.
    """
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step = max(1, total_frames // sample_frames)
    candidates = []

    for f_idx in range(0, min(total_frames, sample_frames * step), step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
        ok, frame = cap.read()
        if not ok:
            break
        res = model(frame, classes=[56, 60], conf=0.18, verbose=False)[0]
        if res.boxes is not None:
            for b in res.boxes.xyxy.cpu().numpy():
                candidates.append(b)

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # Rewind video

    if not candidates:
        return []

    # Cluster detections into distinct spatial zones
    clusters = []
    max_dist = max(width, height) * 0.08  # 8% screen distance

    for box in candidates:
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        matched = False
        for c in clusters:
            c_center = np.mean([((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in c], axis=0)
            if np.linalg.norm(np.array([cx, cy]) - c_center) < max_dist:
                c.append(box)
                matched = True
                break
        if not matched:
            clusters.append([box])

    min_detections = max(3, len(candidates) // (sample_frames * 2))
    persistent = [c for c in clusters if len(c) >= min_detections]

    discovered = []
    for i, c in enumerate(sorted(persistent, key=lambda x: len(x), reverse=True)):
        arr = np.array(c)
        x1 = max(0, int(np.percentile(arr[:, 0], 15) - 10))
        y1 = max(0, int(np.percentile(arr[:, 1], 15) - 10))
        x2 = min(width, int(np.percentile(arr[:, 2], 85) + 10))
        y2 = min(height, int(np.percentile(arr[:, 3], 85) + 10))
        discovered.append({
            "id": i + 1,
            "name": f"Table {i + 1}",
            "box": (x1, y1, x2, y2),
            "capacity": 4 if (x2 - x1) * (y2 - y1) > (width * height * 0.04) else 2,
        })

    return discovered


# Track Analytics State (Sliding Window Movement, 4-state classification)
class TrackAnalytics:
    """
    Maintains per-track behavioral metrics over a temporal window.
    States:
      - UNKNOWN: newly observed, insufficient evidence
      - CUSTOMER: stable residency at a dining table with low movement
      - STAFF: high velocity / walking along corridors without seated residency
      - LOST: track has disappeared
    """

    def __init__(self, track_id: int, window_size: int = 30):
        self.track_id = track_id
        self.positions: deque = deque(maxlen=window_size)  # (frame_idx, cx, cy)
        self.total_frames: int = 0
        self.table_residency: Dict[int, int] = {}  # table_id -> frame_count
        self.current_assigned_table: Optional[int] = None
        self.consecutive_table_frames: int = 0
        self.frames_since_in_zone: int = 0
        self.state: str = "UNKNOWN"
        self.last_seen_frame: int = 0

    def update(
        self,
        frame_idx: int,
        cx: float,
        cy: float,
        current_table_id: Optional[int],
        fps: float,
        screen_scale: float,
    ):
        self.last_seen_frame = frame_idx
        self.total_frames += 1
        self.positions.append((frame_idx, cx, cy))

        # Table zone residency management with hysteresis
        if current_table_id is not None:
            self.table_residency[current_table_id] = self.table_residency.get(current_table_id, 0) + 1
            if current_table_id == self.current_assigned_table:
                self.consecutive_table_frames += 1
                self.frames_since_in_zone = 0
            else:
                # Switching or new assignment requires 6 consecutive frames to avoid jitter
                if self.consecutive_table_frames >= 6 or self.current_assigned_table is None:
                    self.current_assigned_table = current_table_id
                    self.consecutive_table_frames = 1
                self.frames_since_in_zone = 0
        else:
            self.frames_since_in_zone += 1
            # Release table assignment after being absent for 15 frames
            if self.frames_since_in_zone > 15:
                self.current_assigned_table = None
                self.consecutive_table_frames = 0

        # Calculate recent velocity using sliding window (pixels per second normalized)
        recent_speed_pps = 0.0
        if len(self.positions) >= 5:
            dt = (self.positions[-1][0] - self.positions[0][0]) / max(1.0, fps)
            if dt > 0:
                dist = np.hypot(
                    self.positions[-1][1] - self.positions[0][1],
                    self.positions[-1][2] - self.positions[0][2],
                )
                recent_speed_pps = dist / dt

        is_moving_fast = recent_speed_pps > (screen_scale * 0.12)  # moving faster than 12% screen/sec

        # 4-State Role Classification with temporal smoothing
        if self.total_frames < 10:
            self.state = "UNKNOWN"
        else:
            best_table = max(self.table_residency.items(), key=lambda x: x[1]) if self.table_residency else None
            best_table_ratio = (best_table[1] / self.total_frames) if best_table else 0.0

            if self.current_assigned_table is not None and not is_moving_fast and best_table_ratio >= 0.35:
                self.state = "CUSTOMER"
            elif is_moving_fast and best_table_ratio < 0.20:
                self.state = "STAFF"
            elif self.total_frames >= 30 and best_table_ratio < 0.15:
                self.state = "STAFF"
            elif self.state == "UNKNOWN" and best_table_ratio >= 0.50:
                self.state = "CUSTOMER"


# Main DineSight Pipeline
def run_dinesight_pipeline(
    video_path: Path,
    output_path: Path,
    profile_name: Optional[str] = None,
    line_override: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None,
    entry_dir_override: Optional[str] = None,
):
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path.resolve()}")

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path.resolve()}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    screen_scale = max(width, height)

    print("=" * 70)
    print("DineSight: Unified Vision & Counting Analytics")
    print(f"Input Video   : {video_path.resolve()}")
    print(f"Resolution    : {width}x{height} @ {fps:.2f} fps ({total_frames} frames)")
    print(f"Output Video  : {output_path.resolve()}")

    model = YOLO(MODEL_PATH)

    # 1. Scene Profile Setup or Dynamic Discovery
    doorway_line, entry_direction, tables = load_scene_profile(
        profile_name, video_path, width, height
    )

    # Apply command-line overrides if provided
    if line_override:
        doorway_line = (
            (int(line_override[0][0] * width), int(line_override[0][1] * height)),
            (int(line_override[1][0] * width), int(line_override[1][1] * height)),
        )
    elif doorway_line is None:
        # Default doorway crossing line if none configured
        doorway_line = (
            (int(0.50 * width), int(0.10 * height)),
            (int(0.50 * width), int(0.90 * height)),
        )

    if entry_dir_override:
        entry_direction = entry_dir_override

    # Dynamic table discovery fallback if no tables in profile
    if not tables:
        print("[Setup] No pre-defined tables. Running dynamic table auto-discovery...")
        tables = discover_tables_dynamically(cap, model, width, height)
        print(f"[Setup] Discovered {len(tables)} dynamic table zones.")
    else:
        print(f"[Setup] Active Table Zones: {len(tables)}")

    print(f"[Setup] Counting Line: {doorway_line[0]} -> {doorway_line[1]} ({entry_direction})")
    print("=" * 70)

    # Initialize the Unified Counter facade from count.py
    counter = DineSightCounter(
        line_start=doorway_line[0],
        line_end=doorway_line[1],
        entry_direction=entry_direction,
        tolerance_px=max(18, int(width * 0.025)),
        cooldown_frames=12,
        stale_after_frames=90,
        tables=tables,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(output_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height)
    )
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"Could not create output video: {output_path.resolve()}")

    track_analytics: Dict[int, TrackAnalytics] = {}
    frame_idx = 0

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break

            results = model.track(
                frame,
                persist=True,
                tracker=TRACKER_CONFIG if Path(TRACKER_CONFIG).exists() else "bytetrack.yaml",
                classes=[0],
                conf=0.20,
                verbose=False,
            )
            result = results[0]
            annotated = frame.copy()

            boxes = []
            ids = []
            if result.boxes is not None and result.boxes.id is not None:
                boxes = result.boxes.xyxy.cpu().numpy()
                ids = result.boxes.id.int().cpu().tolist()

            # Person detection mapping
            active_track_ids = set(ids)
            table_occupants: Dict[int, List[int]] = {t["id"]: [] for t in tables}
            customers_estimated = 0
            staff_estimated = 0
            unknown_people = 0

            # Periodic cleanup of stale tracks from memory
            if frame_idx % 60 == 0:
                counter.cleanup_stale_tracks(frame_idx)
                stale_ids = [
                    tid for tid, ta in track_analytics.items()
                    if frame_idx - ta.last_seen_frame > 120
                ]
                for tid in stale_ids:
                    del track_analytics[tid]

            for box, tid in zip(boxes, ids):
                x1, y1, x2, y2 = [int(v) for v in box]
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                foot = (cx, y2)

                # 1. Update doorway crossing counter (handled in count.py)
                counter.update_doorway(tid, foot, frame_idx)

                # 2. Check candidate table zone
                candidate_table_id = None
                for t in tables:
                    bx1, by1, bx2, by2 = t["box"]
                    if bx1 <= cx <= bx2 and by1 <= cy <= by2:
                        candidate_table_id = t["id"]
                        break

                # 3. Update track analytics & classification
                if tid not in track_analytics:
                    track_analytics[tid] = TrackAnalytics(tid)

                ta = track_analytics[tid]
                ta.update(frame_idx, cx, cy, candidate_table_id, fps, screen_scale)

                # Assign person to table if confirmed customer
                if ta.state == "CUSTOMER" and ta.current_assigned_table is not None:
                    table_occupants[ta.current_assigned_table].append(tid)
                    customers_estimated += 1
                    role_label = f"Customer #{tid} (T{ta.current_assigned_table})"
                    box_color = (255, 180, 0)  # Cyan/Blue
                elif ta.state == "STAFF":
                    staff_estimated += 1
                    role_label = f"Staff #{tid}"
                    box_color = (0, 215, 255)  # Gold/Yellow
                else:
                    unknown_people += 1
                    role_label = f"Person #{tid}"
                    box_color = (180, 180, 180)  # Neutral grey for UNKNOWN

                # Draw bounding box & badge
                cv2.rectangle(annotated, (x1, y1), (x2, y2), box_color, 2)
                (tw, th), _ = cv2.getTextSize(role_label, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
                cv2.rectangle(annotated, (x1, max(0, y1 - th - 6)), (x1 + tw + 4, max(th + 6, y1)), box_color, -1)
                cv2.putText(
                    annotated,
                    role_label,
                    (x1 + 2, max(th + 2, y1 - 3)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.42,
                    (0, 0, 0),
                    1,
                    cv2.LINE_AA,
                )

            # 4. Evaluate Table Statuses via count.py
            table_summary = counter.evaluate_tables(table_occupants)
            empty_tables = table_summary["empty_tables"]
            occupied_tables = table_summary["occupied_tables"]
            full_tables = table_summary["full_tables"]

            # Render Table Boxes
            for t in tables:
                bx1, by1, bx2, by2 = t["box"]
                t_info = table_summary["table_statuses"][t["id"]]
                count_val = t_info["count"]
                cap_val = t_info["capacity"]
                status_str = t_info["status"]

                if status_str == "EMPTY":
                    t_color = (0, 255, 0)
                    badge = f"{t['name']}: EMPTY (0/{cap_val})"
                elif status_str == "FULL":
                    t_color = (0, 0, 255)
                    badge = f"{t['name']}: FULL ({count_val}/{cap_val})"
                else:
                    t_color = (0, 165, 255)
                    badge = f"{t['name']}: OCCUPIED ({count_val}/{cap_val})"

                cv2.rectangle(annotated, (bx1, by1), (bx2, by2), t_color, 2)
                (stw, sth), _ = cv2.getTextSize(badge, cv2.FONT_HERSHEY_SIMPLEX, 0.40, 1)
                cv2.rectangle(annotated, (bx1, by1), (bx1 + stw + 6, by1 + sth + 8), (20, 20, 20), -1)
                cv2.putText(annotated, badge, (bx1 + 3, by1 + sth + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.40, t_color, 1, cv2.LINE_AA)

            # Render Doorway Line
            cv2.line(annotated, doorway_line[0], doorway_line[1], (255, 255, 0), 3)
            cv2.circle(annotated, doorway_line[0], 6, (0, 255, 255), -1)
            cv2.circle(annotated, doorway_line[1], 6, (0, 255, 255), -1)
            line_mid = ((doorway_line[0][0] + doorway_line[1][0]) // 2, (doorway_line[0][1] + doorway_line[1][1]) // 2)
            cv2.putText(annotated, "DOORWAY", (line_mid[0] + 6, line_mid[1]), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 0), 1, cv2.LINE_AA)

            # 5. Render Explicit Metrics HUD
            doorway_stats = counter.get_summary()
            entries = doorway_stats["entries"]
            exits = doorway_stats["exits"]
            net_change = doorway_stats["net_change"]

            hud_w, hud_h = max(360, int(width * 0.42)), 175
            overlay = annotated.copy()
            cv2.rectangle(overlay, (12, 12), (12 + hud_w, 12 + hud_h), (18, 18, 18), -1)
            cv2.addWeighted(overlay, 0.85, annotated, 0.15, 0, annotated)
            cv2.rectangle(annotated, (12, 12), (12 + hud_w, 12 + hud_h), (100, 100, 100), 1)

            cv2.putText(annotated, "DINESIGHT VISION ANALYTICS", (20, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(annotated, f"Doorway Flow  : Entries={entries} | Exits={exits} (Net Change: {net_change:+d})", (20, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 255), 1, cv2.LINE_AA)
            cv2.putText(annotated, f"Detected Now  : {len(boxes)} boxes ({len(active_track_ids)} active tracks)", (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1, cv2.LINE_AA)
            cv2.putText(annotated, f"Classification: Customers={customers_estimated} | Staff={staff_estimated} | Unknown={unknown_people}", (20, 102), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 215, 0), 1, cv2.LINE_AA)
            cv2.putText(annotated, f"Tables Total  : {len(tables)} (Active: {len(occupied_tables) + len(full_tables)}, Empty: {len(empty_tables)})", (20, 126), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 165, 255), 1, cv2.LINE_AA)
            empty_list_str = f"Empty Tables  : {', '.join(empty_tables[:3])}" if empty_tables else "Empty Tables  : None"
            cv2.putText(annotated, empty_list_str, (20, 150), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 0), 1, cv2.LINE_AA)

            writer.write(annotated)
            frame_idx += 1

            if frame_idx % 75 == 0 or frame_idx == total_frames:
                print(
                    f"Frame {frame_idx:3d}/{total_frames}: "
                    f"Entries={entries}, Exits={exits}, Net={net_change:+d} | "
                    f"Cust={customers_estimated}, Staff={staff_estimated}, Unk={unknown_people} | "
                    f"Empty Tables={len(empty_tables)}"
                )

    finally:
        cap.release()
        writer.release()

    final_stats = counter.get_summary()
    print("\n" + "=" * 70)
    print("DineSight Analysis Complete")
    print(f"Total Processed Frames : {frame_idx}")
    print(f"Doorway Entries        : {final_stats['entries']}")
    print(f"Doorway Exits          : {final_stats['exits']}")
    print(f"Net Change             : {final_stats['net_change']:+d}")
    print(f"Total Tables Monitored : {len(tables)}")
    print(f"Empty Tables Count     : {len(empty_tables)} -> {empty_tables}")
    print(f"Occupied Tables Count  : {len(occupied_tables) + len(full_tables)}")
    print(f"Output Video Saved     : {output_path.resolve()}")
    print("=" * 70)


# CLI Entry Point
def main():
    parser = argparse.ArgumentParser(description="DineSight Unified Vision Analytics")
    parser.add_argument("video_path", nargs="?", default=str(DEFAULT_VIDEO), help="Input video file path")
    parser.add_argument("output_path", nargs="?", default=str(DEFAULT_OUTPUT), help="Output video file path")
    parser.add_argument("--profile", default=None, help="Scene profile name or JSON path (e.g. 'case', 'test2', 'test')")
    parser.add_argument("--line", default=None, help="Doorway line normalized 'x1,y1,x2,y2' (0.0 to 1.0)")
    parser.add_argument("--entry-dir", default=None, choices=["left_to_right", "right_to_left"], help="Entry direction across line")
    args = parser.parse_args()

    line_override = None
    if args.line:
        coords = [float(v) for v in args.line.split(",")]
        line_override = ((coords[0], coords[1]), (coords[2], coords[3]))

    run_dinesight_pipeline(
        video_path=Path(args.video_path),
        output_path=Path(args.output_path),
        profile_name=args.profile,
        line_override=line_override,
        entry_dir_override=args.entry_dir,
    )


if __name__ == "__main__":
    main()
