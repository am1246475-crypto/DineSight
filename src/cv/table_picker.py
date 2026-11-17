#!/usr/bin/env python3
"""
cv/table_picker.py  --  Interactive manual table polygon annotation tool

Allows manual definition of table polygons by clicking vertices on a video frame.
Saves normalized coordinates to tables.json and exports a preview image.
Supports --api-url to PUT /tables directly.

Controls:
    Left Click       : Add point to current table polygon
    Right Click      : Undo last point
    'n' / Enter / Space: Finish current table and start next (T1 -> T2 -> ...)
    'd'              : Delete last completed table
    'c'              : Clear points for current in-progress table
    's'              : Save to JSON & preview and exit
    'q' / ESC        : Quit without saving
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import cv2
import numpy as np


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


class TablePicker:
    def __init__(
        self,
        frame: np.ndarray,
        output_json: str = "tables.json",
        preview_path: str = "tables_preview.jpg",
        initial_tables: Dict[str, List[List[float]]] | None = None,
        api_url: str | None = None,
    ) -> None:
        self.frame = frame
        self.h, self.w = frame.shape[:2]
        self.output_json = output_json
        self.preview_path = preview_path
        self.api_url = api_url

        self.tables: Dict[str, List[Tuple[int, int]]] = {}
        self.current_points: List[Tuple[int, int]] = []

        if initial_tables:
            for t_id, poly in initial_tables.items():
                px_poly = [(int(x * self.w), int(y * self.h)) for x, y in poly]
                self.tables[t_id] = px_poly

        self.window_name = "DineSight Table Picker"

    def _mouse_callback(self, event: int, x: int, y: int, flags: int, param: any) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            self.current_points.append((x, y))
            print(f"Point added: ({x}, {y}) [total in current table: {len(self.current_points)}]")
        elif event == cv2.EVENT_RBUTTONDOWN:
            if self.current_points:
                removed = self.current_points.pop()
                print(f"Point undone: {removed}")

    def _get_next_table_name(self) -> str:
        idx = 1
        while f"T{idx}" in self.tables:
            idx += 1
        return f"T{idx}"

    def _render_display(self) -> np.ndarray:
        display = self.frame.copy()

        # Draw already finalized tables
        for t_id, pts in self.tables.items():
            if len(pts) >= 3:
                pts_arr = np.array(pts, dtype=np.int32)
                cv2.polylines(display, [pts_arr], isClosed=True, color=(0, 255, 0), thickness=2)
                # Semi-transparent overlay fill
                overlay = display.copy()
                cv2.fillPoly(overlay, [pts_arr], color=(0, 220, 0))
                cv2.addWeighted(overlay, 0.2, display, 0.8, 0, display)

                # Label
                lbl_x, lbl_y = pts[0]
                cv2.putText(
                    display,
                    t_id,
                    (lbl_x, max(20, lbl_y - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 255, 0),
                    2,
                    cv2.LINE_AA,
                )

        # Draw current in-progress points
        for i, pt in enumerate(self.current_points):
            cv2.circle(display, pt, 4, (0, 0, 255), -1)
            if i > 0:
                cv2.line(display, self.current_points[i - 1], pt, (0, 0, 255), 2)

        if len(self.current_points) >= 3:
            # Ghost line back to start
            cv2.line(display, self.current_points[-1], self.current_points[0], (0, 165, 255), 1)

        # Instruction overlay
        status_text = (
            f"Tables: {list(self.tables.keys())} | Current table: {self._get_next_table_name()} "
            f"({len(self.current_points)} pts)"
        )
        help_text = "[Click]: Add Pt | [Right-Click]: Undo | [Space/N]: Next Table | [S]: Save | [Q]: Quit"

        cv2.rectangle(display, (0, 0), (self.w, 42), (20, 20, 20), -1)
        cv2.putText(
            display,
            status_text,
            (10, 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        cv2.putText(
            display,
            help_text,
            (10, 36),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (0, 255, 255),
            1,
            cv2.LINE_AA,
        )

        return display

    def run(self) -> Dict[str, List[List[float]]]:
        print("\n=== DineSight Table Picker ===")
        print("Left Click : Add polygon vertex")
        print("Right Click: Undo vertex")
        print("Space / 'n': Finish current table (minimum 3 points)")
        print("'c'        : Clear current points")
        print("'d'        : Delete last finished table")
        print("'s'        : Save to tables.json and tables_preview.jpg")
        print("'q' / ESC  : Quit without saving")
        print("===============================\n")

        try:
            cv2.namedWindow(self.window_name)
            cv2.setMouseCallback(self.window_name, self._mouse_callback)
        except cv2.error as e:
            print(f"Cannot initialize GUI window: {e}", file=sys.stderr)
            return self._build_normalized_result()

        while True:
            display = self._render_display()
            cv2.imshow(self.window_name, display)
            key = cv2.waitKey(20) & 0xFF

            if key in (27, ord("q"), ord("Q")):
                print("Exiting without saving.")
                break
            elif key in (32, ord("n"), ord("N"), 10, 13):  # Space, n, Enter
                if len(self.current_points) >= 3:
                    name = self._get_next_table_name()
                    self.tables[name] = list(self.current_points)
                    print(f"-> Table {name} created with {len(self.current_points)} vertices.")
                    self.current_points = []
                else:
                    print("Need at least 3 points to create a table polygon!")
            elif key in (ord("c"), ord("C")):
                self.current_points.clear()
                print("Cleared current in-progress points.")
            elif key in (ord("d"), ord("D")):
                if self.tables:
                    last_k = list(self.tables.keys())[-1]
                    del self.tables[last_k]
                    print(f"Deleted table {last_k}.")
            elif key in (ord("s"), ord("S")):
                # If there's an in-progress table with >= 3 points, include it
                if len(self.current_points) >= 3:
                    name = self._get_next_table_name()
                    self.tables[name] = list(self.current_points)
                    self.current_points = []

                if not self.tables:
                    print("No tables defined yet! Define at least one table before saving.")
                    continue

                res = self.save()
                cv2.destroyAllWindows()
                return res

        cv2.destroyAllWindows()
        return self._build_normalized_result()

    def _build_normalized_result(self) -> Dict[str, List[List[float]]]:
        normalized: Dict[str, List[List[float]]] = {}
        for t_id, pts in self.tables.items():
            poly = [
                [
                    round(max(0.0, min(1.0, float(x) / self.w)), 4),
                    round(max(0.0, min(1.0, float(y) / self.h)), 4),
                ]
                for x, y in pts
            ]
            normalized[t_id] = poly
        return normalized

    def save(self) -> Dict[str, List[List[float]]]:
        norm_result = self._build_normalized_result()

        # Save JSON
        out_p = Path(self.output_json)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            json.dump(norm_result, f, indent=2)
        print(f"[table_picker] Saved {len(norm_result)} tables to {self.output_json}")

        # Save preview
        preview_img = self.frame.copy()
        for t_id, pts in self.tables.items():
            pts_arr = np.array(pts, dtype=np.int32)
            cv2.polylines(preview_img, [pts_arr], isClosed=True, color=(0, 255, 0), thickness=2)
            lbl_x, lbl_y = pts[0]
            cv2.putText(
                preview_img,
                t_id,
                (lbl_x, max(20, lbl_y - 6)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )

        prev_p = Path(self.preview_path)
        prev_p.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(prev_p), preview_img)
        print(f"[table_picker] Saved preview image to {self.preview_path}")

        # Publish to API if requested
        if self.api_url:
            publish_tables_to_api(self.api_url, norm_result)

        return norm_result


def main() -> None:
    parser = argparse.ArgumentParser(description="Manual Table Polygon Picker")
    parser.add_argument("--video", type=str, default="case.mp4", help="Video file or image")
    parser.add_argument("--frame-idx", type=int, default=0, help="Frame number to annotate")
    parser.add_argument("--output", type=str, default="tables.json", help="Output JSON path")
    parser.add_argument("--preview", type=str, default="tables_preview.jpg", help="Output preview image path")
    parser.add_argument("--load", type=str, default=None, help="Existing tables.json to load and edit")
    parser.add_argument("--api-url", type=str, default=None, help="Optional API URL for PUT /tables")

    args = parser.parse_args()

    # Load initial frame
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"Error opening video {args.video}", file=sys.stderr)
        sys.exit(1)

    cap.set(cv2.CAP_PROP_POS_FRAMES, args.frame_idx)
    ok, frame = cap.read()
    cap.release()

    if not ok or frame is None:
        print(f"Error reading frame {args.frame_idx} from {args.video}", file=sys.stderr)
        sys.exit(1)

    initial_tables = None
    if args.load and Path(args.load).exists():
        with open(args.load, "r", encoding="utf-8") as f:
            initial_tables = json.load(f)

    picker = TablePicker(
        frame=frame,
        output_json=args.output,
        preview_path=args.preview,
        initial_tables=initial_tables,
        api_url=args.api_url,
    )
    picker.run()


if __name__ == "__main__":
    main()

