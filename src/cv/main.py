"""
cv/main.py  --  CV pipeline for ONE camera:
    source (file / RTSP / webcam) -> detect() -> Tracker.update() -> analyzer -> events -> API

    --mode counting  (default)  entrance camera: ZoneCounter  -> "enter" / "exit" events
    --mode tables               wide camera:     TableMonitor -> "table_occupied" / "table_free" events
    --mode room                 wide camera:     RoomCounter  -> "room_count" event every ~10 s (people in the hall)

Run it from inside the cv/ folder (it imports detection.py, tracking.py, counting.py, tables.py):

    # test on a video file, no backend yet (events are printed and saved to events.jsonl)
    python main.py --source test.mp4 --show --realtime

    # set your own zones (normalized 0..1, quote the value because of the ';')
    python main.py --source test.mp4 --show \
        --outside "0,0;0.3,0;0.3,1;0,1" --door "0.3,0;0.7,0;0.7,1;0.3,1" --inside "0.7,0;1,0;1,1;0.7,1"

    # wide camera: table occupancy (tables.json = {"T1": [[x, y], ...], ...}, normalized 0..1)
    python main.py --mode tables --source hall.mp4 --tables-file tables.json --show --realtime
    python main.py --mode tables --source hall.mp4 --table "T1=0.4,0.4;0.6,0.4;0.6,0.7;0.4,0.7" --show

    # wide camera: people in the room (hall polygon normalized 0..1, default = the whole frame)
    python main.py --mode room --source hall.mp4 --show --realtime
    python main.py --mode room --source hall.mp4 --room "0.05,0.1;0.95,0.1;0.95,0.95;0.05,0.95" --show

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
import numpy as np

from counting import DEFAULT_DOOR, DEFAULT_INSIDE, DEFAULT_OUTSIDE, ZoneCounter, parse_polygon
from detection import detect, load_model
from room_count import DEFAULT_ROOM, RoomCounter, load_room_file
from tables import TableMonitor, load_tables_file, parse_table
from tracking import Tracker


# ====================================================================== resize
def resize_frame(frame, target_width: int):
    """Shrink a frame to `target_width` (keeping the aspect ratio). Never enlarges.
    Detection, tracking, counting and drawing all use this SAME resized frame, so the
    line, the boxes and the pixel margins always live in one coordinate system."""
    h, w = frame.shape[:2]
    if target_width <= 0 or w <= target_width:
        return frame
    scale = target_width / float(w)
    return cv2.resize(frame, (target_width, max(1, int(round(h * scale)))), interpolation=cv2.INTER_AREA)


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


ZONE_COLORS = {"outside": (70, 70, 255), "door": (0, 215, 255), "inside": (70, 220, 70)}   # BGR


def draw_overlay(frame, tracks: List[Dict[str, Any]], counter: ZoneCounter, fps: float) -> None:
    h, w = frame.shape[:2]

    # semi-transparent zones + outlines + names
    layer = frame.copy()
    polys = {}
    for name, poly in counter.polygons.items():
        pts = np.array([(int(x * w), int(y * h)) for x, y in poly], dtype=np.int32)
        polys[name] = pts
        cv2.fillPoly(layer, [pts], ZONE_COLORS[name])
    cv2.addWeighted(layer, 0.25, frame, 0.75, 0, frame)
    for name, pts in polys.items():
        cv2.polylines(frame, [pts], True, ZONE_COLORS[name], 2)
        x0, y0 = pts.min(axis=0)
        cv2.putText(frame, name.upper(), (int(x0) + 6, int(y0) + 48), cv2.FONT_HERSHEY_SIMPLEX, 0.6, ZONE_COLORS[name], 2)

    for trk in tracks:
        x1, y1, x2, y2 = [int(round(v)) for v in trk["bbox"]]
        tid = int(trk["track_id"])
        col = _color(tid)
        zone = counter._state.get(tid, {}).get("stable_zone") or "?"      # zone the counter currently believes
        cv2.rectangle(frame, (x1, y1), (x2, y2), col, 2)
        label = f"ID {tid} [{zone}]"
        (tw, _th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
        cv2.putText(frame, label, (max(0, min(x1, w - tw - 2)), max(44, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 2)
        ax_pt, ay_pt = counter._anchor_point(trk["bbox"])
        cv2.circle(frame, (int(ax_pt), int(ay_pt)), 4, (0, 0, 255), -1)

    text = f"IN {counter.entered}   OUT {counter.exited}   NOW {counter.current}   FPS {fps:.1f}"
    cv2.rectangle(frame, (0, 0), (w, 28), (0, 0, 0), -1)
    cv2.putText(frame, text, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)


STATUS_COLORS = {"occupied": (60, 60, 255), "available": (70, 200, 70)}   # BGR: red / green


def draw_tables_overlay(frame, tracks: List[Dict[str, Any]], monitor: TableMonitor, fps: float, ts: float) -> None:
    h, w = frame.shape[:2]
    status = monitor.status()

    layer = frame.copy()
    polys = {}
    for name, poly in monitor.polygons.items():
        pts = np.array([(int(x * w), int(y * h)) for x, y in poly], dtype=np.int32)
        polys[name] = pts
        cv2.fillPoly(layer, [pts], STATUS_COLORS[status[name]["status"]])
    cv2.addWeighted(layer, 0.30, frame, 0.70, 0, frame)

    for name, pts in polys.items():
        st = status[name]
        col = STATUS_COLORS[st["status"]]
        cv2.polylines(frame, [pts], True, col, 2)
        x0, y0 = pts.min(axis=0)
        if st["status"] == "occupied":
            label = f"{name} OCCUPIED {int(ts - st['since'])}s ({st['people']})"
        else:
            label = f"{name} FREE ({st['people']})"
        cv2.putText(frame, label, (int(x0) + 4, max(44, int(y0) + 18)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 2)

    for trk in tracks:
        x1, y1, x2, y2 = [int(round(v)) for v in trk["bbox"]]
        cv2.rectangle(frame, (x1, y1), (x2, y2), (230, 230, 230), 1)
        ax_pt, ay_pt = monitor._anchor_point(trk["bbox"])
        cv2.circle(frame, (int(ax_pt), int(ay_pt)), 4, (0, 0, 255), -1)

    text = f"TABLES occupied {monitor.occupied_count}/{len(status)}   FPS {fps:.1f}"
    cv2.rectangle(frame, (0, 0), (w, 28), (0, 0, 0), -1)
    cv2.putText(frame, text, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)


ROOM_COLOR = (255, 160, 40)   # BGR


def draw_room_overlay(frame, tracks: List[Dict[str, Any]], room: RoomCounter, fps: float) -> None:
    h, w = frame.shape[:2]
    pts = np.array([(int(x * w), int(y * h)) for x, y in room.polygon], dtype=np.int32)
    layer = frame.copy()
    cv2.fillPoly(layer, [pts], ROOM_COLOR)
    cv2.addWeighted(layer, 0.15, frame, 0.85, 0, frame)
    cv2.polylines(frame, [pts], True, ROOM_COLOR, 2)

    inside = set(room.inside_ids(tracks, frame.shape))
    for trk in tracks:
        x1, y1, x2, y2 = [int(round(v)) for v in trk["bbox"]]
        is_in = trk.get("track_id") in inside
        col = (70, 220, 70) if is_in else (150, 150, 150)
        cv2.rectangle(frame, (x1, y1), (x2, y2), col, 2 if is_in else 1)
        ax_pt, ay_pt = room._anchor_point(trk["bbox"])
        cv2.circle(frame, (int(ax_pt), int(ay_pt)), 4, (0, 0, 255), -1)

    text = f"ROOM people {room.stable_count}  (raw {room.raw_count})   FPS {fps:.1f}"
    cv2.rectangle(frame, (0, 0), (w, 28), (0, 0, 0), -1)
    cv2.putText(frame, text, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)


# ====================================================================== main loop
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CV pipeline for one camera: entry/exit counting or table occupancy")
    p.add_argument("--source", required=True, help="video file, rtsp://... URL, or webcam index (0)")
    p.add_argument("--mode", choices=["counting", "tables", "room"], default="counting",
                   help="counting = entrance camera, tables = wide camera (table occupancy), room = wide camera (people in the room)")
    p.add_argument("--camera-id", default=None, help="camera_id written in every event (default: entrance, or wide in tables / room mode)")
    p.add_argument("--outside", type=parse_polygon, default=DEFAULT_OUTSIDE, help="outside zone: 'x1,y1;x2,y2;x3,y3;...' normalized 0..1")
    p.add_argument("--door", type=parse_polygon, default=DEFAULT_DOOR, help="door zone (the area people must pass through)")
    p.add_argument("--inside", type=parse_polygon, default=DEFAULT_INSIDE, help="inside zone")
    p.add_argument("--anchor", choices=["bottom", "center"], default=None, help="which point of the box is tested (default: bottom for counting, center for tables / room)")
    p.add_argument("--tables-file", default=None, help="tables mode: JSON file {\"T1\": [[x, y], ...], ...} (normalized 0..1)")
    p.add_argument("--table", type=parse_table, action="append", help="tables mode: one table 'T1=x1,y1;x2,y2;x3,y3' (repeat for more)")
    p.add_argument("--occupy-after", type=float, default=5.0, help="tables mode: seconds someone must stay before a table is OCCUPIED")
    p.add_argument("--free-after", type=float, default=20.0, help="tables mode: seconds a table must be empty before it is FREE")
    p.add_argument("--gap-tolerance", type=float, default=2.0, help="tables mode: missed-detection gaps shorter than this (s) are ignored")
    p.add_argument("--room", type=parse_polygon, default=None, help="room mode: hall polygon 'x1,y1;x2,y2;x3,y3;...' normalized 0..1 (default: the whole frame)")
    p.add_argument("--room-file", default=None, help='room mode: JSON file [[x, y], ...] or {"room": [[x, y], ...]} (normalized 0..1)')
    p.add_argument("--count-window", type=float, default=4.0, help="room mode: seconds of readings the median is taken over (3-5)")
    p.add_argument("--count-interval", type=float, default=10.0, help="room mode: seconds between room_count events")
    p.add_argument("--stable-frames", type=int, default=3, help="frames a track must stay in a zone before the zone change is accepted")
    p.add_argument("--door-timeout", type=int, default=90, help="frames after which an unfinished door crossing is cancelled")
    p.add_argument("--width", type=int, default=768, help="resize every frame to this width before processing (0 = no resize, never enlarges)")
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
    if args.camera_id is None:
        args.camera_id = "wide" if args.mode in ("tables", "room") else "entrance"
    if args.anchor is None:
        args.anchor = "center" if args.mode in ("tables", "room") else "bottom"

    counter: Optional[ZoneCounter] = None
    monitor: Optional[TableMonitor] = None
    room: Optional[RoomCounter] = None
    if args.mode == "tables":
        tables: Dict[str, Any] = {}
        if args.tables_file:
            tables.update(load_tables_file(args.tables_file))
        for name, poly in args.table or []:
            tables[name] = poly
        if not tables:
            raise SystemExit("--mode tables needs --tables-file tables.json or at least one --table T1=x,y;x,y;x,y")
        monitor = TableMonitor(
            tables, camera_id=args.camera_id, anchor=args.anchor,
            occupy_after_s=args.occupy_after, free_after_s=args.free_after, gap_tolerance_s=args.gap_tolerance,
        )
        print(f"[init] mode=tables  tables={sorted(tables)}")
    elif args.mode == "room":
        polygon = load_room_file(args.room_file) if args.room_file else (args.room or DEFAULT_ROOM)
        room = RoomCounter(
            polygon, camera_id=args.camera_id, anchor=args.anchor,
            window_s=args.count_window, interval_s=args.count_interval,
        )
        print(f"[init] mode=room  polygon={len(polygon)} points  window={args.count_window}s  every={args.count_interval}s")
    else:
        counter = ZoneCounter(
            camera_id=args.camera_id, outside=args.outside, door=args.door, inside=args.inside,
            anchor=args.anchor, stable_frames=args.stable_frames, door_timeout=args.door_timeout,
        )

    print("[init] loading detector ...")
    load_model(args.weights)
    tracker = Tracker(device=args.device, with_reid=args.with_reid)
    sender = EventSender(args.api_url, args.events_file or None)
    source = FrameSource(args.source, realtime=args.realtime, loop=args.loop)
    print(f"[init] source={args.source}  camera_id={args.camera_id}  "
          f"api={'OFF (dry run)' if not args.api_url else args.api_url}")

    writer: Optional[cv2.VideoWriter] = None
    writer_size: Tuple[int, int] = (0, 0)
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

            frame = resize_frame(frame, args.width)      # every frame, so the line always matches it
            t0 = time.time()
            dets = detect(frame, conf=args.conf, imgsz=args.imgsz)       # Contract 1
            tracks = tracker.update(dets, frame)                         # Contract 2 (CV-2 order: dets, frame)
            ts = time.time() if source.is_stream else idx / source.fps     # seconds (video time for files)
            if monitor is not None:
                new_events = monitor.update(tracks, frame.shape, ts)
            elif room is not None:
                new_events = room.update(tracks, frame.shape, ts)
            else:
                new_events = counter.update(tracks, frame.shape, frame_idx=idx)   # Contract 3
            for ev in new_events:
                sender.send(ev)
                if room is not None:
                    print(f"[event] {ev['event_type']:14s} people={ev['meta']['people']}  (video {ts:.1f}s)")
                elif monitor is not None:
                    print(f"[event] {ev['event_type']:14s} table={ev['meta']['table_id']}  "
                          f"people={ev['meta']['people']}  (video {ev['meta']['video_s']}s)")
                else:
                    print(f"[event] {ev['event_type']:5s} track={ev['track_id']}  "
                          f"({ev['meta']['transition']})  IN={counter.entered} OUT={counter.exited}")

            dt = time.time() - t0
            inst = 1.0 / dt if dt > 0 else 0.0
            fps_ema = inst if fps_ema == 0 else 0.9 * fps_ema + 0.1 * inst

            if args.show or args.out_video:
                if monitor is not None:
                    draw_tables_overlay(frame, tracks, monitor, fps_ema, ts)
                elif room is not None:
                    draw_room_overlay(frame, tracks, room, fps_ema)
                else:
                    draw_overlay(frame, tracks, counter, fps_ema)
                if args.out_video:
                    if writer is None:
                        h, w = frame.shape[:2]
                        writer_size = (w, h)
                        writer = cv2.VideoWriter(
                            args.out_video, cv2.VideoWriter_fourcc(*"MJPG"),
                            max(1.0, source.fps / args.frame_skip), writer_size,
                        )
                    out_frame = frame if (frame.shape[1], frame.shape[0]) == writer_size else cv2.resize(frame, writer_size)
                    writer.write(out_frame)
                if args.show:
                    cv2.imshow("tables" if monitor is not None else "room" if room is not None else "entry/exit", frame)
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
        if room is not None:
            print(f"[done] frames read={idx + 1}  people now={room.stable_count}  "
                  f"sent={sender.sent}  rejected={sender.rejected}")
        elif monitor is not None:
            print(f"[done] frames read={idx + 1}  tables occupied now={monitor.occupied_count}/{len(monitor.polygons)}  "
                  f"sent={sender.sent}  rejected={sender.rejected}")
        else:
            print(f"[done] frames read={idx + 1}  entered={counter.entered}  exited={counter.exited}  "
                  f"sent={sender.sent}  rejected={sender.rejected}")


if __name__ == "__main__":
    main()