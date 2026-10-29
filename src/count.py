"""
DineSight Counting Module (src/count.py)

All counting logic lives here:
  - Doorway entry / exit counting with a per-track state machine that
      * uses a dead-band around the line so detector jitter cannot flip sides,
      * detects crossings geometrically (segment intersection), so fast walkers
        and low-fps video are not missed,
      * ignores people who walk around the ends of the line,
      * cancels quick back-and-forth reversals instead of double counting,
      * hands a track's state to a new ID when the tracker re-assigns IDs
        (re-identification by time + position).
  - Stale-track cleanup for IDs that disappear.
  - Table headcount with unique-ID deduplication, status classification and
    short-term occupancy memory (no flicker when a detection drops out).
  - DineSightCounter: single public API that combines both.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

Point = Tuple[float, float]

# Direction a person must travel across the line to count as an ENTRY.
_DIRECTION_VECTORS: Dict[str, Point] = {
    "left_to_right": (1.0, 0.0),
    "right_to_left": (-1.0, 0.0),
    "top_to_bottom": (0.0, 1.0),
    "bottom_to_top": (0.0, -1.0),
}


# ----------------------------------------------------------------------
# Geometric helpers
# ----------------------------------------------------------------------
def signed_distance(point: Point, start: Point, end: Point) -> float:
    """Signed perpendicular distance (pixels) from *point* to the infinite line start->end."""
    px, py = point
    x1, y1 = start
    x2, y2 = end
    length = math.hypot(x2 - x1, y2 - y1)
    if length == 0:
        return 0.0
    return ((x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)) / length


def signed_side(point: Point, start: Point, end: Point) -> int:
    """Return +1 / -1 / 0 depending on which side of *start->end* the point lies."""
    d = signed_distance(point, start, end)
    return 1 if d > 0 else (-1 if d < 0 else 0)


def distance_to_segment(point: Point, start: Point, end: Point) -> float:
    """Minimum Euclidean distance from *point* to segment *start->end*."""
    px, py = point
    x1, y1 = start
    x2, y2 = end
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def line_crossing_point(p0: Point, p1: Point, start: Point, end: Point) -> Optional[Point]:
    """Point where the movement p0->p1 meets the infinite line start->end (None if it does not)."""
    d0 = signed_distance(p0, start, end)
    d1 = signed_distance(p1, start, end)
    if d0 == d1 or d0 * d1 > 0:
        return None
    t = d0 / (d0 - d1)
    return (p0[0] + t * (p1[0] - p0[0]), p0[1] + t * (p1[1] - p0[1]))


def resolve_entry_side(
    start: Point,
    end: Point,
    direction: str = "left_to_right",
    vector: Optional[Point] = None,
) -> int:
    """
    Return the side (+1 / -1) of the line a person ends up on when ENTERING.

    Works for any line orientation (the old code only handled perfectly vertical
    lines drawn top->bottom).  *vector* (dx, dy) overrides the named direction.
    """
    if vector is None:
        if direction not in _DIRECTION_VECTORS:
            raise ValueError(
                f"Unknown entry direction '{direction}'. "
                f"Use one of {sorted(_DIRECTION_VECTORS)} or pass entry_vector."
            )
        vector = _DIRECTION_VECTORS[direction]
    vx, vy = vector
    norm = math.hypot(vx, vy)
    if norm == 0:
        raise ValueError("entry vector must be non-zero")
    vx, vy = vx / norm, vy / norm
    mx, my = (start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0
    d = signed_distance((mx + vx * 1000.0, my + vy * 1000.0), start, end)  # = 1000 * (v . normal)
    if abs(d) < 100.0:  # |v . normal| < 0.1 -> direction (almost) parallel to the line
        raise ValueError(
            f"Entry direction '{direction}' is parallel to the doorway line, so it cannot define "
            "an entry side. Use top_to_bottom / bottom_to_top for a horizontal line, "
            "left_to_right / right_to_left for a vertical one, or pass an explicit vector."
        )
    return 1 if d > 0 else -1


# ----------------------------------------------------------------------
# Per-track crossing state
# ----------------------------------------------------------------------
class _TrackCrossingState:
    """Internal bookkeeping for one tracked person relative to the line."""

    __slots__ = (
        "committed_side",       # side of the line the person is (confidently) on
        "anchor_point",         # last foot-point seen on the committed side
        "last_position",        # last foot-point
        "last_seen_frame",      # frame index of last observation
        "last_crossing_frame",  # frame when the last crossing was counted
        "last_event",           # "ENTRY" / "EXIT" / None  (last counted crossing)
        "entry_count",
        "exit_count",
    )

    def __init__(self) -> None:
        self.committed_side: Optional[int] = None
        self.anchor_point: Optional[Point] = None
        self.last_position: Optional[Point] = None
        self.last_seen_frame: int = 0
        self.last_crossing_frame: int = -10**9
        self.last_event: Optional[str] = None
        self.entry_count: int = 0
        self.exit_count: int = 0


# ----------------------------------------------------------------------
# LineCrossingCounter
# ----------------------------------------------------------------------
class LineCrossingCounter:
    """
    Counts people crossing a doorway line with directional flow.

    Parameters
    ----------
    tolerance_px:
        How far past either END of the line segment a crossing may happen and
        still count (people walking around the line are ignored).
    dead_band_px:
        A person must be at least this far from the line to be considered "on a
        side".  Jitter inside the band never flips a side.  Default: tolerance/2.
    cooldown_frames:
        Reversal window.  If the same ID crosses back within this many frames,
        the earlier crossing is cancelled (it was jitter / a head-poke), instead
        of counting an entry AND an exit.
    stale_after_frames:
        Track states unseen for this long are purged.
    entry_vector:
        Optional (dx, dy) giving the entry direction for lines at any angle.
    reid_window_frames / reid_distance_px:
        When a brand-new ID appears within *reid_distance_px* of an ID that
        vanished 2..reid_window_frames frames earlier, it inherits that ID's
        state, so ID switches do not lose crossings.  0 disables.
    """

    def __init__(
        self,
        line_start: Tuple[int, int],
        line_end: Tuple[int, int],
        entry_direction: str = "left_to_right",
        tolerance_px: int = 18,
        cooldown_frames: int = 10,
        stale_after_frames: int = 90,
        dead_band_px: Optional[float] = None,
        entry_vector: Optional[Point] = None,
        reid_window_frames: int = 0,
        reid_distance_px: float = 60.0,
        verbose: bool = True,
    ) -> None:
        self.line_start = line_start
        self.line_end = line_end
        self.entry_direction = entry_direction
        self.entry_vector = entry_vector
        self.tolerance_px = tolerance_px
        self.cooldown_frames = cooldown_frames
        self.stale_after_frames = stale_after_frames
        self.dead_band_px = float(dead_band_px if dead_band_px is not None else max(3.0, tolerance_px * 0.5))
        self.reid_window_frames = reid_window_frames
        self.reid_distance_px = reid_distance_px
        self.verbose = verbose

        self._entry_side = resolve_entry_side(line_start, line_end, entry_direction, entry_vector)

        self.entries: int = 0
        self.exits: int = 0
        self.events: List[Dict] = []
        self._tracks: Dict[int, _TrackCrossingState] = {}

    
    # Public API
    def update_track(
        self,
        track_id: int,
        foot_point: Point,
        frame_idx: int = 0,
    ) -> Optional[Tuple[str, int]]:
        """
        Feed a new observation for *track_id*.

        Returns ``("ENTRY", total_entries)`` or ``("EXIT", total_exits)`` when a
        confirmed crossing occurs, otherwise ``None``.
        """
        state = self._tracks.get(track_id)
        if state is None:
            state = self._spawn_state(foot_point, frame_idx)
            self._tracks[track_id] = state

        state.last_seen_frame = frame_idx
        d = signed_distance(foot_point, self.line_start, self.line_end)

        # First sighting right on the line (typical for a door at the frame border):
        # use the raw side as a provisional origin so the first real crossing is not lost.
        if state.committed_side is None and abs(d) >= 1.0:
            state.committed_side = 1 if d > 0 else -1
            state.anchor_point = foot_point

        event: Optional[Tuple[str, int]] = None
        if abs(d) >= self.dead_band_px:
            side = 1 if d > 0 else -1
            if state.committed_side is not None and side != state.committed_side:
                event = self._on_side_flip(state, track_id, foot_point, side, frame_idx)
            state.committed_side = side
            state.anchor_point = foot_point

        state.last_position = foot_point
        return event

    def cleanup_stale(self, current_frame: int) -> List[int]:
        """Remove tracks not seen for ``stale_after_frames``. Returns purged IDs."""
        stale_ids = [
            tid for tid, st in self._tracks.items()
            if current_frame - st.last_seen_frame > self.stale_after_frames
        ]
        for tid in stale_ids:
            del self._tracks[tid]
        return stale_ids

    @property
    def net_change(self) -> int:
        """Entries minus exits since counting began."""
        return self.entries - self.exits

    def get_stats(self) -> Dict[str, int]:
        return {"entries": self.entries, "exits": self.exits, "net_change": self.net_change}

    # Internal helpers
    def _spawn_state(self, foot_point: Point, frame_idx: int) -> _TrackCrossingState:
        """Create state for a new ID, inheriting from a recently lost ID when one matches."""
        state = _TrackCrossingState()
        if self.reid_window_frames <= 0:
            return state

        best_tid: Optional[int] = None
        best_dist = self.reid_distance_px
        for tid, old in self._tracks.items():
            gap = frame_idx - old.last_seen_frame
            # gap < 2: that ID is probably still active (just not updated yet this frame)
            if gap < 2 or gap > self.reid_window_frames or old.last_position is None:
                continue
            dist = math.hypot(old.last_position[0] - foot_point[0], old.last_position[1] - foot_point[1])
            if dist <= best_dist:
                best_tid, best_dist = tid, dist

        if best_tid is not None:
            old = self._tracks.pop(best_tid)
            state.committed_side = old.committed_side
            state.anchor_point = old.anchor_point
            state.last_position = old.last_position
            state.last_crossing_frame = old.last_crossing_frame
            state.last_event = old.last_event
            state.entry_count = old.entry_count
            state.exit_count = old.exit_count
        return state

    def _on_side_flip(
        self,
        state: _TrackCrossingState,
        track_id: int,
        point: Point,
        new_side: int,
        frame_idx: int,
    ) -> Optional[Tuple[str, int]]:
        anchor = state.anchor_point or state.last_position
        if anchor is None:
            return None

        # Where did the path anchor -> point actually meet the line? It must be on the
        # doorway segment (plus tolerance), otherwise the person walked around it.
        crossing = line_crossing_point(anchor, point, self.line_start, self.line_end)
        if crossing is None:
            return None
        if distance_to_segment(crossing, self.line_start, self.line_end) > self.tolerance_px:
            return None

        kind = "ENTRY" if new_side == self._entry_side else "EXIT"

        # Quick reversal of the previous crossing -> it was jitter, cancel it.
        if frame_idx - state.last_crossing_frame < self.cooldown_frames:
            if state.last_event is not None and state.last_event != kind:
                self._cancel_last(state, track_id, frame_idx)
            return None

        state.last_crossing_frame = frame_idx
        state.last_event = kind
        if kind == "ENTRY":
            self.entries += 1
            state.entry_count += 1
            total = self.entries
        else:
            self.exits += 1
            state.exit_count += 1
            total = self.exits

        self.events.append({"frame": frame_idx, "track_id": track_id, "type": kind})
        if self.verbose:
            print(f"[Frame {frame_idx:4d}] {kind} COUNTED: ID={track_id}, total={total}")
        return (kind, total)

    def _cancel_last(self, state: _TrackCrossingState, track_id: int, frame_idx: int) -> None:
        if state.last_event == "ENTRY":
            self.entries = max(0, self.entries - 1)
            state.entry_count = max(0, state.entry_count - 1)
        elif state.last_event == "EXIT":
            self.exits = max(0, self.exits - 1)
            state.exit_count = max(0, state.exit_count - 1)
        self.events.append({"frame": frame_idx, "track_id": track_id, "type": f"CANCEL_{state.last_event}"})
        if self.verbose:
            print(f"[Frame {frame_idx:4d}] reversal: cancelled {state.last_event} for ID={track_id}")
        state.last_event = None
        state.last_crossing_frame = -10**9  # a genuine re-crossing right after must count



# TableOccupancyTracker
class TableOccupancyTracker:
    """
    Evaluates per-table headcounts and classifies tables as EMPTY / OCCUPIED / FULL.

    Upstream code provides ``table_occupancy``: ``table_id -> list[track_id]``.
    This class deduplicates IDs and computes status.  With ``hold_frames > 0`` and a
    ``frame_idx``, a table that just lost all its occupants keeps its last headcount
    for that many frames, so a missed detection does not flash the table EMPTY.
    """

    def __init__(self, tables: List[Dict], hold_frames: int = 0) -> None:
        self.tables = tables
        self.hold_frames = hold_frames
        self._last_occupied: Dict[int, Tuple[List[int], int]] = {}

    def evaluate_tables(
        self,
        table_occupancy: Dict[int, List[int]],
        frame_idx: Optional[int] = None,
    ) -> Dict:
        empty_tables: List[str] = []
        occupied_tables: List[str] = []
        full_tables: List[str] = []
        table_statuses: Dict[int, Dict] = {}

        for t in self.tables:
            t_id = t["id"]
            seated_ids = sorted(set(table_occupancy.get(t_id, [])))

            if frame_idx is not None and self.hold_frames > 0:
                if seated_ids:
                    self._last_occupied[t_id] = (seated_ids, frame_idx)
                elif t_id in self._last_occupied:
                    prev_ids, prev_frame = self._last_occupied[t_id]
                    if frame_idx - prev_frame <= self.hold_frames:
                        seated_ids = prev_ids

            count = len(seated_ids)
            capacity = t.get("capacity", 4)

            if count == 0:
                empty_tables.append(t["name"])
                status = "EMPTY"
            elif count >= capacity:
                full_tables.append(t["name"])
                status = "FULL"
            else:
                occupied_tables.append(t["name"])
                status = "OCCUPIED"

            table_statuses[t_id] = {
                "name": t["name"],
                "count": count,
                "capacity": capacity,
                "status": status,
                "seated_ids": seated_ids,
            }

        return {
            "empty_tables": empty_tables,
            "occupied_tables": occupied_tables,
            "full_tables": full_tables,
            "table_statuses": table_statuses,
            "total_tables": len(self.tables),
            "total_empty": len(empty_tables),
            "total_active": len(occupied_tables) + len(full_tables),
        }



# DineSightCounter: single public integration API
class DineSightCounter:
    """
    Facade owning a :class:`LineCrossingCounter` and a :class:`TableOccupancyTracker`
    and exposing one clean interface for ``detect.py`` and future integrations.
    """

    def __init__(
        self,
        line_start: Tuple[int, int],
        line_end: Tuple[int, int],
        entry_direction: str = "left_to_right",
        tolerance_px: int = 18,
        cooldown_frames: int = 10,
        stale_after_frames: int = 90,
        tables: Optional[List[Dict]] = None,
        dead_band_px: Optional[float] = None,
        entry_vector: Optional[Point] = None,
        reid_window_frames: int = 0,
        reid_distance_px: float = 60.0,
        table_hold_frames: int = 0,
        verbose: bool = True,
    ) -> None:
        self.doorway = LineCrossingCounter(
            line_start,
            line_end,
            entry_direction,
            tolerance_px,
            cooldown_frames,
            stale_after_frames,
            dead_band_px=dead_band_px,
            entry_vector=entry_vector,
            reid_window_frames=reid_window_frames,
            reid_distance_px=reid_distance_px,
            verbose=verbose,
        )
        self.table_tracker = TableOccupancyTracker(tables or [], hold_frames=table_hold_frames)

    # -- Doorway ---------------------------------------------------------
    def update_doorway(
        self,
        track_id: int,
        foot_point: Point,
        frame_idx: int = 0,
    ) -> Optional[Tuple[str, int]]:
        return self.doorway.update_track(track_id, foot_point, frame_idx)

    def cleanup_stale_tracks(self, current_frame: int) -> List[int]:
        return self.doorway.cleanup_stale(current_frame)

    # -- Tables ----------------------------------------------------------
    def evaluate_tables(
        self,
        table_occupancy: Dict[int, List[int]],
        frame_idx: Optional[int] = None,
    ) -> Dict:
        return self.table_tracker.evaluate_tables(table_occupancy, frame_idx)

    # -- Summary ---------------------------------------------------------
    def get_summary(self) -> Dict:
        stats = self.doorway.get_stats()
        return {
            "entries": stats["entries"],
            "exits": stats["exits"],
            "net_change": stats["net_change"],
        }

    def get_events(self) -> List[Dict]:
        return list(self.doorway.events)