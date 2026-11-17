#!/usr/bin/env python3
"""
cv/table_detect.py  --  Automatic table detection and polygon generation

Detects dining tables (and optionally chairs) across sampled frames,
merges recurring detections using IoU, expands boxes by ~15% to enclose chairs/patrons,
and saves normalized polygon coordinates to tables.json and a preview image.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import cv2
import numpy as np

# Ensure cv directory is on path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from detection import detect, load_model


def compute_iou(box_a: Sequence[float], box_b: Sequence[float]) -> float:
    """Compute Intersection-over-Union between two boxes [x1, y1, x2, y2]."""
    xA = max(box_a[0], box_b[0])
    yA = max(box_a[1], box_b[1])
    xB = min(box_a[2], box_b[2])
    yB = min(box_a[3], box_b[3])

    inter_w = max(0.0, xB - xA)
    inter_h = max(0.0, yB - yA)
    inter_area = inter_w * inter_h

    area_a = max(0.0, (box_a[2] - box_a[0]) * (box_a[3] - box_a[1]))
    area_b = max(0.0, (box_b[2] - box_b[0]) * (box_b[3] - box_b[1]))

    union = area_a + area_b - inter_area
    if union <= 0.0:
        return 0.0
    return inter_area / union


def cluster_boxes(
    boxes: List[Sequence[float]],
    iou_thresh: float = 0.35,
    min_count: int = 2,
) -> List[List[float]]:
    """Group overlapping boxes across frames and return averaged box per cluster."""
    clusters: List[List[Sequence[float]]] = []

    for box in boxes:
        matched = False
        for cluster in clusters:
            # Check IoU against cluster centroid or members
            cluster_avg = [
                sum(b[i] for b in cluster) / len(cluster) for i in range(4)
            ]
            if compute_iou(box, cluster_avg) >= iou_thresh:
                cluster.append(box)
                matched = True
                break
        if not matched:
            clusters.append([box])

    # Filter by minimum detections to avoid random false positives
    valid_clusters = [c for c in clusters if len(c) >= min_count]
    if not valid_clusters and clusters:
        # If no cluster met min_count, keep top clusters by size
        clusters.sort(key=lambda c: len(c), reverse=True)
        valid_clusters = clusters[:5]

    averaged: List[List[float]] = []
    for c in valid_clusters:
        avg_box = [
            sum(b[i] for b in c) / len(c) for i in range(4)
        ]
        averaged.append(avg_box)

    return averaged


def expand_box(
    box: Sequence[float],
    frame_w: int,
    frame_h: int,
    factor: float = 0.15,
) -> List[float]:
    """Expand box coordinates by a factor (e.g., 15%) to include chairs and seated people."""
    x1, y1, x2, y2 = box
    w = x2 - x1
    h = y2 - y1
    dx = w * factor
    dy = h * factor

    return [
        max(0.0, x1 - dx),
        max(0.0, y1 - dy),
        min(float(frame_w), x2 + dx),
        min(float(frame_h), y2 + dy),
    ]


def sort_tables(boxes: List[List[float]]) -> List[List[float]]:
    """
    Sort tables primarily top-to-bottom, left-to-right.
    Group tables with similar y-center into rows for intuitive T1, T2 ordering.
    """
    if not boxes:
        return []

    # Sort by y center first
    with_centers = []
    for b in boxes:
        cx = (b[0] + b[2]) / 2.0
        cy = (b[1] + b[3]) / 2.0
        with_centers.append((cy, cx, b))

    with_centers.sort(key=lambda item: item[0])

    # Row clustering by y coordinate (within 20% of frame height)
    rows: List[List[Tuple[float, float, List[float]]]] = []
    for item in with_centers:
        cy, cx, b = item
        placed = False
        for row in rows:
            row_avg_y = sum(r[0] for r in row) / len(row)
            box_h = b[3] - b[1]
            if abs(cy - row_avg_y) < (box_h * 0.6):
                row.append(item)
                placed = True
                break
        if not placed:
            rows.append([item])

    sorted_boxes: List[List[float]] = []
    for row in rows:
        # Sort each row left-to-right by cx
        row.sort(key=lambda item: item[1])
        for _, _, b in row:
            sorted_boxes.append(b)

    return sorted_boxes


def publish_tables_to_api(api_url: str, tables_data: Dict[str, List[List[float]]]) -> bool:
    """Send PUT request to API with tables data."""
    try:
        url = api_url.rstrip("/")
        if not url.endswith("/tables"):
            url = f"{url}/tables"
        payload = json.dumps(tables_data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            print(f"[API] Published tables to {url} (HTTP {status})")
            return status in (200, 201, 204)
    except Exception as exc:
        print(f"[API ERROR] Failed to publish tables to {api_url}: {exc}", file=sys.stderr)
        return False


def run_table_detect(
    video_path: str,
    output_json: str = "tables.json",
    preview_path: str = "tables_preview.jpg",
    num_samples: int = 25,
    conf: float = 0.25,
    iou_thresh: float = 0.35,
    min_count: int = 2,
    expand: float = 0.15,
    classes: Sequence[int] = (60,),
    api_url: str | None = None,
) -> Dict[str, List[List[float]]]:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    if total_frames <= 0 or frame_w <= 0 or frame_h <= 0:
        raise RuntimeError("Invalid video metadata")

    print(f"[table_detect] Video: {video_path} ({frame_w}x{frame_h}, {total_frames} frames)")
    print(f"[table_detect] Sampling {num_samples} frames for classes {list(classes)} (conf={conf})...")

    # Sample uniformly spaced frame indices
    if total_frames <= num_samples:
        sample_indices = list(range(total_frames))
    else:
        sample_indices = [
            int(i * (total_frames - 1) / (num_samples - 1)) for i in range(num_samples)
        ]

    load_model()

    raw_boxes: List[List[float]] = []
    preview_frame = None

    for idx in sample_indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue

        if preview_frame is None:
            preview_frame = frame.copy()

        dets = detect(frame, conf=conf, classes=classes)
        for d in dets:
            raw_boxes.append(d["bbox"])

    cap.release()
    print(f"[table_detect] Collected {len(raw_boxes)} candidate table detections across samples.")

    # Cluster recurring boxes
    merged_boxes = cluster_boxes(raw_boxes, iou_thresh=iou_thresh, min_count=min_count)
    print(f"[table_detect] Identified {len(merged_boxes)} stable table clusters.")

    # Expand boxes to include chairs/seated patrons
    expanded_boxes = [expand_box(b, frame_w, frame_h, factor=expand) for b in merged_boxes]

    # Sort tables top-to-bottom, left-to-right
    sorted_boxes = sort_tables(expanded_boxes)

    # Build normalized polygon format: {"T1": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]], ...}
    tables_result: Dict[str, List[List[float]]] = {}
    for i, b in enumerate(sorted_boxes, start=1):
        t_id = f"T{i}"
        x1_norm = round(max(0.0, min(1.0, b[0] / frame_w)), 4)
        y1_norm = round(max(0.0, min(1.0, b[1] / frame_h)), 4)
        x2_norm = round(max(0.0, min(1.0, b[2] / frame_w)), 4)
        y2_norm = round(max(0.0, min(1.0, b[3] / frame_h)), 4)

        # 4 vertices: top-left, top-right, bottom-right, bottom-left
        poly = [
            [x1_norm, y1_norm],
            [x2_norm, y1_norm],
            [x2_norm, y2_norm],
            [x1_norm, y2_norm],
        ]
        tables_result[t_id] = poly

    # Save tables.json
    output_file = Path(output_json)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(tables_result, f, indent=2)
    print(f"[table_detect] Saved {len(tables_result)} tables to {output_json}")

    # Generate preview image
    if preview_frame is not None:
        draw_img = preview_frame.copy()
        for t_id, poly in tables_result.items():
            pts_px = np.array(
                [[int(x * frame_w), int(y * frame_h)] for x, y in poly],
                dtype=np.int32,
            )
            cv2.polylines(draw_img, [pts_px], isClosed=True, color=(0, 255, 0), thickness=2)
            # Label near top-left vertex
            lbl_x = pts_px[0][0]
            lbl_y = max(18, pts_px[0][1] - 6)
            cv2.putText(
                draw_img,
                t_id,
                (lbl_x, lbl_y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )

        preview_file = Path(preview_path)
        preview_file.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(preview_file), draw_img)
        print(f"[table_detect] Saved preview image to {preview_path}")

    # Optionally publish to API
    if api_url:
        publish_tables_to_api(api_url, tables_result)

    return tables_result


def main() -> None:
    parser = argparse.ArgumentParser(description="DineSight Table Detection and Extraction")
    parser.add_argument("--video", type=str, default="case.mp4", help="Path to video file")
    parser.add_argument("--output", type=str, default="tables.json", help="Path to output JSON")
    parser.add_argument("--preview", type=str, default="tables_preview.jpg", help="Path to preview image")
    parser.add_argument("--samples", type=int, default=25, help="Number of frames to sample")
    parser.add_argument("--conf", type=float, default=0.25, help="Confidence threshold")
    parser.add_argument("--iou", type=float, default=0.35, help="IoU threshold for clustering")
    parser.add_argument("--min-count", type=int, default=2, help="Minimum detections to keep a cluster")
    parser.add_argument("--expand", type=float, default=0.15, help="Expansion factor (0.15 = 15%)")
    parser.add_argument(
        "--include-chairs",
        action="store_true",
        help="Include COCO chair class 56 alongside dining table class 60",
    )
    parser.add_argument("--api-url", type=str, default=None, help="Optional API URL for PUT /tables")

    args = parser.parse_args()

    classes = [60, 56] if args.include_chairs else [60]

    run_table_detect(
        video_path=args.video,
        output_json=args.output,
        preview_path=args.preview,
        num_samples=args.samples,
        conf=args.conf,
        iou_thresh=args.iou,
        min_count=args.min_count,
        expand=args.expand,
        classes=classes,
        api_url=args.api_url,
    )


if __name__ == "__main__":
    main()

