"""
Tests for DineSight counting logic (tests/test_count.py).
Validates:
- Line crossing geometry (signed_side, distance_to_segment)
- LineCrossingCounter: single entry, single exit, re-crossing with same ID, cooldown/jitter prevention
- TableOccupancyTracker: deduplication of table occupant IDs, capacity, empty/occupied/full statuses
- Stale track cleanup
"""

import unittest
from src.count import (
    signed_side,
    distance_to_segment,
    LineCrossingCounter,
    TableOccupancyTracker,
    DineSightCounter,
)


class TestGeometry(unittest.TestCase):
    def test_signed_side_vertical(self):
        # Line from (100, 0) to (100, 200) - vertical pointing down
        start = (100, 0)
        end = (100, 200)
        # Left of line: px < 100
        # cross = (x2 - x1)*(py - y1) - (y2 - y1)*(px - x1)
        # cross = 0 - (200 - 0)*(50 - 100) = -200 * -50 = +10000 > 0 -> side = 1
        self.assertEqual(signed_side((50, 100), start, end), 1)
        # Right of line: px > 100
        # cross = 0 - 200 * (150 - 100) = -10000 < 0 -> side = -1
        self.assertEqual(signed_side((150, 100), start, end), -1)
        # On line
        self.assertEqual(signed_side((100, 100), start, end), 0)

    def test_distance_to_segment(self):
        start = (100, 0)
        end = (100, 200)
        # Perpendicular distance
        self.assertAlmostEqual(distance_to_segment((120, 100), start, end), 20.0)
        # Point beyond end
        self.assertAlmostEqual(distance_to_segment((100, 250), start, end), 50.0)


class TestLineCrossingCounter(unittest.TestCase):
    def test_single_entry_and_exit(self):
        # Vertical line top-to-bottom at x=100
        # For vertical line with left_to_right:
        # side=1 is left, side=-1 is right. So crossing from 1 to -1 is an entry.
        counter = LineCrossingCounter(
            line_start=(100, 0),
            line_end=(100, 200),
            entry_direction="left_to_right",
            tolerance_px=20,
            cooldown_frames=5,
        )

        # Track 1 approaches line from left
        counter.update_track(1, (90, 100), frame_idx=1)
        self.assertEqual(counter.entries, 0)

        # Track 1 crosses to right
        event = counter.update_track(1, (110, 100), frame_idx=2)
        self.assertEqual(event, ("ENTRY", 1))
        self.assertEqual(counter.entries, 1)
        self.assertEqual(counter.net_change, 1)

        # Track 1 moves further right (past cooldown)
        counter.update_track(1, (130, 100), frame_idx=10)
        self.assertEqual(counter.entries, 1)

        # Track 1 crosses back to left (Exit)
        event = counter.update_track(1, (90, 100), frame_idx=15)
        self.assertEqual(event, ("EXIT", 1))
        self.assertEqual(counter.exits, 1)
        self.assertEqual(counter.net_change, 0)

    def test_cooldown_prevents_jitter_near_line(self):
        counter = LineCrossingCounter(
            line_start=(100, 0),
            line_end=(100, 200),
            entry_direction="left_to_right",
            tolerance_px=20,
            cooldown_frames=10,
        )

        # Cross left to right -> Entry
        counter.update_track(1, (90, 100), frame_idx=1)
        event = counter.update_track(1, (110, 100), frame_idx=2)
        self.assertEqual(event, ("ENTRY", 1))

        # Jitter across line immediately within cooldown (frame 3)
        event = counter.update_track(1, (95, 100), frame_idx=3)
        self.assertIsNone(event)
        self.assertEqual(counter.exits, 0)

    def test_reentry_allowed_after_cooldown(self):
        counter = LineCrossingCounter(
            line_start=(100, 0),
            line_end=(100, 200),
            entry_direction="left_to_right",
            tolerance_px=20,
            cooldown_frames=5,
        )

        # Entry 1
        counter.update_track(1, (90, 100), frame_idx=1)
        counter.update_track(1, (110, 100), frame_idx=2)
        self.assertEqual(counter.entries, 1)

        # Exit 1
        counter.update_track(1, (90, 100), frame_idx=10)
        self.assertEqual(counter.exits, 1)

        # Re-entry 2 with same track ID! (Previously locked by permanent set)
        event = counter.update_track(1, (110, 100), frame_idx=20)
        self.assertEqual(event, ("ENTRY", 2))
        self.assertEqual(counter.entries, 2)
        self.assertEqual(counter.net_change, 1)

    def test_stale_track_cleanup(self):
        counter = LineCrossingCounter(
            line_start=(100, 0),
            line_end=(100, 200),
            stale_after_frames=30,
        )
        counter.update_track(10, (50, 50), frame_idx=1)
        self.assertIn(10, counter._tracks)

        purged = counter.cleanup_stale(current_frame=40)
        self.assertEqual(purged, [10])
        self.assertNotIn(10, counter._tracks)


class TestTableOccupancyTracker(unittest.TestCase):
    def test_table_deduplication_and_status(self):
        tables = [
            {"id": 1, "name": "Table 1", "capacity": 2},
            {"id": 2, "name": "Table 2", "capacity": 4},
        ]
        tracker = TableOccupancyTracker(tables)

        # Test duplicate IDs in list
        raw_occupancy = {
            1: [101, 101, 102],  # Duplicate 101 -> actual count should be 2 (FULL)
            2: [],                # Empty
        }
        res = tracker.evaluate_tables(raw_occupancy)

        self.assertEqual(res["table_statuses"][1]["count"], 2)
        self.assertEqual(res["table_statuses"][1]["status"], "FULL")
        self.assertEqual(res["table_statuses"][2]["status"], "EMPTY")
        self.assertIn("Table 2", res["empty_tables"])
        self.assertIn("Table 1", res["full_tables"])


if __name__ == "__main__":
    unittest.main()

