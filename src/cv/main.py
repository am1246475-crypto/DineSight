"""
cv/main.py  --  CV pipeline for ONE camera:
    source (file / RTSP / webcam) -> detect() -> Tracker.update() -> LineCounter -> events -> API

Run it from inside the cv/ folder (it imports detection.py, tracking.py, counting.py):

    # test on a video file, no backend yet (events are printed and saved to events.jsonl)
    python main.py --source test.mp4 --show --realtime

    # RTSP camera (or the local MediaMTX test stream), send events to the backend
    python main.py --source rtsp://localhost:8554/entrance --camera-id entrance \
                   --api-url http://localhost:8000/events --frame-skip 2

Keys: press 'q' in the preview window to stop (or Ctrl+C in the terminal).
"""
from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import cv2

from counting import DEFAULT_LINE, LineCounter, parse_line
from detection import detect, load_model
from tracking import Tracker


# ====================================================================== frame source
class FrameSource:
    """Video file, webcam index, or RTSP/HTTP stream.

    * Files   : every frame is returned, in order (optionally paced to the real fps).
    * Streams : a reader thread always keeps only the LATEST frame, so a slow model
                never builds up delay, and the connection is reopened if it drops.
    """

    STREAM_PREFIXES = ("rtsp://", "rtmp://", "http://", "https://")

    def __init__(self, source: str, realtime: bool = False, loop: bool = False, reconnect_delay: float = 2.0):
        self.source: Any = int(source) if source.isdigit() else source
        self.is_stream = isinstance(self.source, int) or str(source).lower().startswith(self.STREAM_PREFIXES)
        self.realtime = realtime and not self.is_stream
        self.loop = loop and not self.is_stream
        self.reconnect_delay = reconnect_delay

        self._cap: Optional[cv2.VideoCapture] = None
        self._next_t: Optional[float] = None
        self._cond = threading.Condition()
        self._latest = None
        self._seq = 0
        self._returned = 0
        self._stopped = False
        self._thread: Optional[threading.Thread] = None

        opened = self._open()
        if self.is_stream:
            self._thread = threading.Thread(target=self._reader, daemon=True)
            self._thread.start()
        elif not opened:
            raise RuntimeError(f"Could not open video file: {source}")

    @property
    def fps(self) -> float:
        fps = self._cap.get(cv2.CAP_PROP_FPS) if self._cap is not None else 0.0
        return fps if fps and fps > 1 else 25.0

    def _open(self) -> bool:
        if self._cap is not None:
            self._cap.release()
        if self.is_stream and isinstance(self.source, str):
            cap = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG)
        else:
            cap = cv2.VideoCapture(self.source)
        if self.is_stream:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._cap = cap
        return cap.isOpened()

    def _reader(self) -> None:
        while not self._stopped:
            if self._cap is None or not self._cap.isOpened():
                print(f"[source] (re)connecting to {self.source} ...")
                time.sleep(self.reconnect_delay)
                self._open()
                continue
            ok, frame = self._cap.read()
            if not ok:
                print("[source] read failed, will reconnect")
                self._cap.release()
                continue
            with self._cond:
                self._latest = frame
                self._seq += 1
                self._cond.notify_all()

    def read(self) -> Tuple[bool, Any]:
        """Returns (ok, frame). ok=False means the source ended / was stopped.
        For streams frame can be None when no new frame arrived in 1 second."""
        if self.is_stream:
            with self._cond:
                if self._seq == self._returned:
                    self._cond.wait(timeout=1.0)
                if self._stopped:
                    return False, None
                if self._seq == self._returned:
                    return True, None
                self._returned = self._seq
                return True, self._latest

        assert self._cap is not None
        ok, frame = self._cap.read()
        if not ok and self.loop:
            self._open()
            ok, frame = self._cap.read()
        if not ok:
            return False, None
        if self.realtime:
            if self._next_t is None:
                self._next_t = time.time()
            delay = self._next_t - time.time()
            if delay > 0:
                time.sleep(delay)
            self._next_t += 1.0 / self.fps
        return True, frame

    def close(self) -> None:
        self._stopped = True
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._cap is not None:
            self._cap.release()


# ====================================================================== event sender
class EventSender:
    """Sends events to POST /events from a background thread, so a slow or dead API
    never blocks the video processing.

    * Every event is appended to a .jsonl file first (nothing is lost if the API is down).
    * If api_url is None: dry-run mode (only print + .jsonl).
    * Network errors / 5xx  -> retry with backoff (1s, 2s, ... max 10s).
    * 4xx (API rejected the event) -> logged and dropped, no endless retry.
    """

    def __init__(self, api_url: Optional[str], jsonl_path: Optional[str], max_queue: int = 1000, timeout: float = 3.0):
        self.api_url = api_url
        self.jsonl_path = jsonl_path
        self.timeout = timeout
        self.sent = 0
        self.rejected = 0
        self._file_lock = threading.Lock()
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def send(self, event: Dict[str, Any]) -> None:
        if self.jsonl_path:
            with self._file_lock, open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        if not self.api_url:
            return
        try:
            self._q.put_nowait(event)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            self._q.put_nowait(event)
            print("[sender] queue full: dropped the oldest event (it is still in the .jsonl file)")

    def _post(self, event: Dict[str, Any]) -> bool:
        """True = done (sent or permanently rejected), False = retry."""
        req = urllib.request.Request(
            self.api_url,
            data=json.dumps(event).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if 200 <= resp.status < 300:
                    self.sent += 1
                    return True
                print(f"[sender] unexpected status {resp.status}; will retry")
                return False
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                self.rejected += 1
                print(f"[sender] API rejected the event ({e.code}); dropped. Check the Event schema.")
                return True
            print(f"[sender] API error {e.code}; will retry")
            return False
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"[sender] POST failed ({e}); will retry")
            return False

    def _run(self) -> None:
        while not (self._stop.is_set() and self._q.empty()):
            try:
                event = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            delay, tries = 1.0, 0
            while True:
                tries += 1
                if self._post(event):
                    break
                if self._stop.is_set() and tries >= 2:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 10.0)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=8.0)


# ====================================================================== drawing
def _color(track_id: int) -> Tuple[int, int, int]:
    return (int((track_id * 37) % 255), int((track_id * 17 + 80) % 255), int((track_id * 97 + 40) % 255))


def draw_overlay(frame, tracks: List[Dict[str, Any]], counter: LineCounter, fps: float) -> None:
    h, w = frame.shape[:2]
    (ax, ay), (bx, by) = counter.line
    a = (int(ax * w), int(ay * h))
    b = (int(bx * w), int(by * h))
    cv2.line(frame, a, b, (0, 255, 255), 2)

    # arrow showing the INSIDE side (flip with --invert if it points the wrong way)
    mx, my = (a[0] + b[0]) // 2, (a[1] + b[1]) // 2
    nx, ny = counter.inside_normal(w, h)
    tip = (int(mx + nx * 60), int(my + ny * 60))
    cv2.arrowedLine(frame, (mx, my), tip, (0, 255, 0), 2, tipLength=0.3)
    cv2.putText(frame, "IN", (tip[0] + 5, tip[1]), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

    for trk in tracks:
        x1, y1, x2, y2 = [int(round(v)) for v in trk["bbox"]]
        tid = int(trk["track_id"])
        col = _color(tid)
        cv2.rectangle(frame, (x1, y1), (x2, y2), col, 2)
        cv2.putText(frame, f"ID {tid}", (x1, max(14, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 2)
        ax_pt, ay_pt = counter._anchor_point(trk["bbox"])
        cv2.circle(frame, (int(ax_pt), int(ay_pt)), 4, (0, 0, 255), -1)

    text = f"IN {counter.entered}   OUT {counter.exited}   NOW {counter.current}   FPS {fps:.1f}"
    cv2.rectangle(frame, (0, 0), (w, 28), (0, 0, 0), -1)
    cv2.putText(frame, text, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)


# ====================================================================== main loop
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Entry/exit counting pipeline for one camera")
    p.add_argument("--source", required=True, help="video file, rtsp://... URL, or webcam index (0)")
    p.add_argument("--camera-id", default="entrance", help="camera_id written in every event")
    p.add_argument("--line", type=parse_line, default=DEFAULT_LINE, help="virtual line x1,y1,x2,y2 normalized (default 0.25,0,0.25,1)")
    p.add_argument("--invert", action="store_true", help="swap which side of the line is 'inside'")
    p.add_argument("--anchor", choices=["bottom", "center"], default="bottom", help="which point of the box is tested against the line")
    p.add_argument("--margin", type=float, default=8.0, help="dead band around the line in pixels")
    p.add_argument("--weights", default="rtdetr-l.pt", help="detector weights")
    p.add_argument("--conf", type=float, default=0.4, help="detection confidence")
    p.add_argument("--imgsz", type=int, default=640, help="detector input size")
    p.add_argument("--device", default=None, help="device for the tracker (auto if omitted)")
    p.add_argument("--with-reid", action="store_true", help="enable appearance Re-ID in the tracker (slower)")
    p.add_argument("--frame-skip", type=int, default=1, help="process every Nth frame (1 = all)")
    p.add_argument("--api-url", default=None, help="e.g. http://localhost:8000/events (omit = dry run)")
    p.add_argument("--events-file", default="events.jsonl", help="local copy of every event ('' to disable)")
    p.add_argument("--show", action="store_true", help="show a preview window")
    p.add_argument("--out-video", default=None, help="save the annotated video (e.g. out.avi)")
    p.add_argument("--realtime", action="store_true", help="file input: play at the video's real speed")
    p.add_argument("--loop", action="store_true", help="file input: restart when the video ends")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    args.frame_skip = max(1, args.frame_skip)

    print("[init] loading detector ...")
    load_model(args.weights)
    tracker = Tracker(device=args.device, with_reid=args.with_reid)
    counter = LineCounter(
        camera_id=args.camera_id, line=args.line, margin_px=args.margin,
        anchor=args.anchor, invert=args.invert,
    )
    sender = EventSender(args.api_url, args.events_file or None)
    source = FrameSource(args.source, realtime=args.realtime, loop=args.loop)
    print(f"[init] source={args.source}  camera_id={args.camera_id}  "
          f"api={'OFF (dry run)' if not args.api_url else args.api_url}")

    writer: Optional[cv2.VideoWriter] = None
    fps_ema = 0.0
    idx = -1
    try:
        while True:
            ok, frame = source.read()
            if not ok:
                break
            if frame is None:                       # stream: no new frame yet
                if args.show and (cv2.waitKey(1) & 0xFF) == ord("q"):
                    break
                continue

            idx += 1
            if idx % args.frame_skip:
                continue

            t0 = time.time()
            dets = detect(frame, conf=args.conf, imgsz=args.imgsz)       # Contract 1
            tracks = tracker.update(dets, frame)                         # Contract 2 (CV-2 order: dets, frame)
            for ev in counter.update(tracks, frame.shape, frame_idx=idx):  # Contract 3
                sender.send(ev)
                print(f"[event] {ev['event_type']:5s} track={ev['track_id']}  "
                      f"IN={counter.entered} OUT={counter.exited}")

            dt = time.time() - t0
            inst = 1.0 / dt if dt > 0 else 0.0
            fps_ema = inst if fps_ema == 0 else 0.9 * fps_ema + 0.1 * inst

            if args.show or args.out_video:
                draw_overlay(frame, tracks, counter, fps_ema)
                if args.out_video:
                    if writer is None:
                        h, w = frame.shape[:2]
                        writer = cv2.VideoWriter(
                            args.out_video, cv2.VideoWriter_fourcc(*"MJPG"),
                            max(1.0, source.fps / args.frame_skip), (w, h),
                        )
                    writer.write(frame)
                if args.show:
                    cv2.imshow("entry/exit", frame)
                    if (cv2.waitKey(1) & 0xFF) == ord("q"):
                        break
    except KeyboardInterrupt:
        print("\n[main] stopped by user")
    finally:
        source.close()
        if writer is not None:
            writer.release()
        if args.show:
            cv2.destroyAllWindows()
        sender.stop()
        print(f"[done] frames read={idx + 1}  entered={counter.entered}  exited={counter.exited}  "
              f"sent={sender.sent}  rejected={sender.rejected}")


if __name__ == "__main__":
    main()