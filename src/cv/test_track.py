import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import cv2
import numpy as np

from tracking import Tracker


def get_color_for_id(track_id: int) -> Tuple[int, int, int]:
    """Generate a consistent BGR color for each track ID."""
    return (
        int((track_id * 37) % 255),
        int((track_id * 17 + 80) % 255),
        int((track_id * 97 + 40) % 255),
    )


def load_detections_json(json_path: Path) -> Tuple[Dict[int, List[Dict[str, Any]]], str]:
    """Load detection.json and normalize it into {frame_idx: [detections]}.

    Preserves the root schema style ('list_of_lists', 'list_of_dicts',
    'frames_dict', or 'index_dict') so output JSON matches detection.py.
    """
    with json_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    frame_map: Dict[int, List[Dict[str, Any]]] = {}

    if isinstance(raw, list):
        schema = "list_of_dicts"
        for idx, item in enumerate(raw):
            if isinstance(item, list):
                frame_map[idx] = item
                schema = "list_of_lists"
            else:
                frame_idx = int(item.get("frame_id", item.get("frame", item.get("frame_idx", idx))))
                frame_map[frame_idx] = item.get("detections", item.get("objects", []))
                schema = "list_of_dicts"
        return frame_map, schema

    if isinstance(raw, dict):
        if "frames" in raw and isinstance(raw["frames"], list):
            for idx, item in enumerate(raw["frames"]):
                frame_idx = int(item.get("frame_id", item.get("frame", item.get("frame_idx", idx))))
                frame_map[frame_idx] = item.get("detections", item.get("objects", []))
            return frame_map, "frames_dict"

        for k, v in raw.items():
            if str(k).isdigit() and isinstance(v, list):
                frame_map[int(k)] = v
        return frame_map, "index_dict"

    raise ValueError(f"Unsupported detection.json structure in {json_path}")


def format_output_json(
    tracked_frames: List[Dict[str, Any]],
    schema_style: str,
) -> Any:
    """Format tracked results to match the input detection.json structure."""
    if schema_style == "list_of_lists":
        return [item["detections"] for item in tracked_frames]
    if schema_style == "list_of_dicts":
        return tracked_frames
    if schema_style == "frames_dict":
        return {"frames": tracked_frames}
    return {str(item["frame_id"]): item["detections"] for item in tracked_frames}


def create_video_writer(
    output_video_path: Path, fps: float, width: int, height: int
) -> cv2.VideoWriter:
    """Create VideoWriter using MJPG for .avi (glitch-free) or avc1/mp4v for .mp4."""
    output_video_path.parent.mkdir(parents=True, exist_ok=True)
    codecs = ("MJPG",) if output_video_path.suffix.lower() == ".avi" else ("avc1", "mp4v")
    for codec in codecs:
        fourcc = cv2.VideoWriter_fourcc(*codec)
        writer = cv2.VideoWriter(str(output_video_path), fourcc, fps, (width, height))
        if writer.isOpened():
            return writer
        writer.release()
    raise RuntimeError(f"Could not initialize VideoWriter for {output_video_path}")


def run_tracking_test(
    video_path: Path,
    detections_path: Path,
    output_video_path: Path,
    output_json_path: Path,
    device: str | None = None,
    with_reid: bool = False,
    show: bool = False,
) -> None:
    frame_detections, schema_style = load_detections_json(detections_path)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    writer = create_video_writer(output_video_path, fps, width, height)

    tracker = Tracker(device=device, with_reid=with_reid)
    tracked_frames: List[Dict[str, Any]] = []

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        dets = frame_detections.get(frame_idx, [])
        tracks = tracker.update(dets, frame)

        annotated = np.ascontiguousarray(frame.copy(), dtype=np.uint8)
        for trk in tracks:
            x1, y1, x2, y2 = [int(round(v)) for v in trk["bbox"]]
            tid = trk["track_id"]
            color = get_color_for_id(tid)

            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            label = f"ID: {tid} ({trk['conf']:.2f})"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
            cv2.rectangle(annotated, (x1, max(0, y1 - th - 8)), (x1 + tw + 6, y1), color, -1)
            cv2.putText(
                annotated,
                label,
                (x1 + 3, max(th, y1 - 4)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (255, 255, 255),
                2,
            )

        writer.write(annotated)
        if show:
            cv2.imshow("Tracking Test", annotated)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        tracked_frames.append(
            {
                "frame_id": frame_idx,
                "detections": tracks,
            }
        )
        frame_idx += 1

    cap.release()
    writer.release()
    if show:
        cv2.destroyAllWindows()

    output_json_path.parent.mkdir(parents=True, exist_ok=True)
    output_payload = format_output_json(tracked_frames, schema_style)
    with output_json_path.open("w", encoding="utf-8") as f:
        json.dump(output_payload, f, indent=2)

    print(f"Processed {frame_idx} frames.")
    print(f"Saved annotated video to: {output_video_path}")
    print(f"Saved tracking JSON to: {output_json_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test BoT-SORT tracking on video + detection.json")
    parser.add_argument("--video", type=Path, required=True, help="Path to input video file")
    parser.add_argument("--detections", type=Path, default=Path("detections.json"), help="Path to detections.json")
    parser.add_argument("--out-video", type=Path, default=Path("tracked_output.avi"), help="Output annotated video path")
    parser.add_argument("--out-json", type=Path, default=Path("tracking.json"), help="Output tracking JSON path")
    parser.add_argument("--device", type=str, default=None, help="Device for BoT-SORT (auto-detected if omitted)")
    parser.add_argument("--with-reid", action="store_true", help="Enable appearance Re-ID in BoT-SORT")
    parser.add_argument("--show", action="store_true", help="Display live preview window")
    args = parser.parse_args()

    run_tracking_test(
        video_path=args.video,
        detections_path=args.detections,
        output_video_path=args.out_video,
        output_json_path=args.out_json,
        device=args.device,
        with_reid=args.with_reid,
        show=args.show,
    )