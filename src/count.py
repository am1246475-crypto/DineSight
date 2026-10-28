"""
DineSight Counting Module (src/count.py)

All counting logic lives here:
  - Doorway entry / exit line-crossing counting with a per-track state
    machine that allows legitimate re-crossings while rejecting jitter.
  - Stale-track cleanup for IDs that disappear.
  - Table headcount with unique-ID deduplication and status classification.
  - DineSightCounter — single public API that combines both.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

import numpy as np


# Geometric Helpers
def signed_side(
    point: Tuple[float, float],
    start: Tuple[float, float],
    end: Tuple[float, float],
) -> int:
    """Return +1 / -1 / 0 depending on which side of *start→end* the point lies."""
    px, py = point
    x1, y1 = start
    x2, y2 = end
    cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
    return 1 if cross > 0 else (-1 if cross < 0 else 0)


def distance_to_segment(
    point: Tuple[float, float],
    start: Tuple[float, float],
    end: Tuple[float, float],
) -> float:
    """Minimum Euclidean distance from *point* to segment *start→end*."""
    px, py = point
    x1, y1 = start
    x2, y2 = end
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    nx, ny = x1 + t * dx, y1 + t * dy
    return ((px - nx) ** 2 + (py - ny) ** 2) ** 0.5


# Per-track crossing state
class _TrackCrossingState:
    """Internal bookkeeping for one tracked person relative to the line."""

    __slots__ = (
        "last_side",          # last non-zero side observed
        "last_position",      # last foot-point
        "last_seen_frame",    # frame index of last observation
        "last_crossing_frame",  # frame when last crossing was counted
        "entry_count",        # how many entry crossings this ID has made
        "exit_count",         # how many exit crossings this ID has made
    )

    def __init__(self) -> None:
        self.last_side: Optional[int] = None
        self.last_position: Optional[Tuple[float, float]] = None
        self.last_seen_frame: int = 0
        self.last_crossing_frame: int = -9999
        self.entry_count: int = 0
        self.exit_count: int = 0


# LineCrossingCounter — state-machine doorway counter
class LineCrossingCounter:
    """
    Counts people crossing a doorway line with directional flow.

    Key improvements over the previous implementation:
    * **Re-crossing**: a person who enters, leaves, and enters again produces
      three events (entry, exit, entry) — the permanent-lock is gone.
    * **Cooldown**: after a crossing is counted the same ID must wait
      *cooldown_frames* before another crossing registers, which prevents
      jitter near the line from producing duplicates.
    * **Stale cleanup**: track states that have not been observed for
      *stale_after_frames* are purged so they don't block future IDs.
    """

    def __init__(
        self,
        line_start: Tuple[int, int],
        line_end: Tuple[int, int],
        entry_direction: str = "left_to_right",
        tolerance_px: int = 18,
        cooldown_frames: int = 10,
        stale_after_frames: int = 90,
    ) -> None:
        self.line_start = line_start
        self.line_end = line_end
        self.entry_direction = entry_direction
        self.tolerance_px = tolerance_px
        self.cooldown_frames = cooldown_frames
        self.stale_after_frames = stale_after_frames

        # Global totals
        self.entries: int = 0
        self.exits: int = 0

        # Per-track state (replaces the old counted_entry_ids / counted_exit_ids sets)
        self._tracks: Dict[int, _TrackCrossingState] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def update_track(
        self,
        track_id: int,
        foot_point: Tuple[float, float],
        frame_idx: int = 0,
    ) -> Optional[Tuple[str, int]]:
        """
        Feed a new observation for *track_id*.

        Returns ``("ENTRY", total_entries)`` or ``("EXIT", total_exits)``
        when a confirmed crossing occurs, otherwise ``None``.
        """
        state = self._tracks.get(track_id)
        if state is None:
            state = _TrackCrossingState()
            self._tracks[track_id] = state

        state.last_seen_frame = frame_idx
        side = signed_side(foot_point, self.line_start, self.line_end)

        old_side = state.last_side
        old_point = state.last_position

        event: Optional[Tuple[str, int]] = None

        # Check for a side change that is near the line
        if (
            old_side is not None
            and old_side in (-1, 1)
            and side in (-1, 1)
            and old_side != side
        ):
            near_line = (
                distance_to_segment(foot_point, self.line_start, self.line_end)
                <= self.tolerance_px
                or (
                    old_point is not None
                    and distance_to_segment(old_point, self.line_start, self.line_end)
                    <= self.tolerance_px
                )
            )
            past_cooldown = (
                frame_idx - state.last_crossing_frame >= self.cooldown_frames
            )

            if near_line and past_cooldown:
                is_entry = self._is_entry(old_side, side)

                if is_entry:
                    self.entries += 1
                    state.entry_count += 1
                    state.last_crossing_frame = frame_idx
                    event = ("ENTRY", self.entries)
                    print(
                        f"[Frame {frame_idx:3d}] ENTRY COUNTED: ID={track_id}, "
                        f"total={self.entries}"
                    )
                else:
                    self.exits += 1
                    state.exit_count += 1
                    state.last_crossing_frame = frame_idx
                    event = ("EXIT", self.exits)
                    print(
                        f"[Frame {frame_idx:3d}] EXIT COUNTED: ID={track_id}, "
                        f"total={self.exits}"
                    )

        # Update stored state
        state.last_position = foot_point
        if side != 0:
            state.last_side = side

        return event

    def cleanup_stale(self, current_frame: int) -> List[int]:
        """Remove tracks not seen for ``stale_after_frames``.  Returns purged IDs."""
        stale_ids = [
            tid
            for tid, st in self._tracks.items()
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
        return {
            "entries": self.entries,
            "exits": self.exits,
            "net_change": self.net_change,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _is_entry(self, old_side: int, new_side: int) -> bool:
        """Determine if a side transition is an entry or an exit."""
        is_vertical = (
            self.line_start[0] == self.line_end[0]
            and self.line_start[1] < self.line_end[1]
        )
        if self.entry_direction == "left_to_right":
            return (old_side == 1 and new_side == -1) if is_vertical else (old_side == -1 and new_side == 1)
        else:
            return (old_side == -1 and new_side == 1) if is_vertical else (old_side == 1 and new_side == -1)


# TableOccupancyTracker
class TableOccupancyTracker:
    """
    Evaluates per-table headcounts and classifies tables as
    EMPTY / OCCUPIED / FULL.

    Upstream code is responsible for providing a ``table_occupancy`` mapping
    of ``table_id → list[track_id]``.  This class deduplicates IDs and
    computes status.
    """

    def __init__(self, tables: List[Dict]) -> None:
        self.tables = tables

    def evaluate_tables(self, table_occupancy: Dict[int, List[int]]) -> Dict:
        """Return status summary for every table."""
        empty_tables: List[str] = []
        occupied_tables: List[str] = []
        full_tables: List[str] = []
        table_statuses: Dict[int, Dict] = {}

        for t in self.tables:
            t_id = t["id"]
            # Deduplicate — defensive fix against double-appends upstream
            seated_ids = sorted(set(table_occupancy.get(t_id, [])))
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


# DineSightCounter — single public integration API
class DineSightCounter:
    """
    Unified facade that owns a :class:`LineCrossingCounter` and a
    :class:`TableOccupancyTracker` and exposes one clean interface for
    ``detect.py`` and future integrations.
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
    ) -> None:
        self.doorway = LineCrossingCounter(
            line_start,
            line_end,
            entry_direction,
            tolerance_px,
            cooldown_frames,
            stale_after_frames,
        )
        self.table_tracker = TableOccupancyTracker(tables or [])

    # -- Doorway ---------------------------------------------------------
    def update_doorway(
        self,
        track_id: int,
        foot_point: Tuple[float, float],
        frame_idx: int = 0,
    ) -> Optional[Tuple[str, int]]:
        return self.doorway.update_track(track_id, foot_point, frame_idx)

    def cleanup_stale_tracks(self, current_frame: int) -> List[int]:
        return self.doorway.cleanup_stale(current_frame)

    # -- Tables ----------------------------------------------------------
    def evaluate_tables(self, table_occupancy: Dict[int, List[int]]) -> Dict:
        return self.table_tracker.evaluate_tables(table_occupancy)

    # -- Summary ---------------------------------------------------------
    def get_summary(self) -> Dict:
        stats = self.doorway.get_stats()
        return {
            "entries": stats["entries"],
            "exits": stats["exits"],
            "net_change": stats["net_change"],
        }
