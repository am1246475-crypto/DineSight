
from datetime import datetime, timezone
import os
from pathlib import Path
import threading
from contextlib import contextmanager

import psycopg
from psycopg.types.json import Jsonb
from dotenv import load_dotenv


# Load database configuration
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing from .env")

_db_lock = threading.Lock()
_conn = None


@contextmanager
def get_connection():
    global _conn
    with _db_lock:
        if _conn is None or _conn.closed:
            _conn = psycopg.connect(DATABASE_URL)
        else:
            try:
                _conn.execute("SELECT 1")
            except Exception:
                try:
                    _conn.close()
                except Exception:
                    pass
                _conn = psycopg.connect(DATABASE_URL)
        with _conn.transaction():
            yield _conn


def parse_timestamp(value):
    if value is None:
        return datetime.now(timezone.utc)

    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value


def save_event(event):
    """Save an event and update related database tables."""

    event_type = event["event_type"]
    camera_id = event["camera_id"]
    track_id = event.get("track_id")
    meta = event.get("meta") or {}

    if not isinstance(meta, dict):
        raise ValueError("meta must be a dictionary")

    if not camera_id:
        raise ValueError("camera_id is required")

    timestamp = parse_timestamp(
        event.get("timestamp") or event.get("ts")
    )

    allowed_types = {
        "enter",
        "exit",
        "table_occupied",
        "table_free",
        "room_count",
    }

    if event_type not in allowed_types:
        raise ValueError("Unsupported event_type")

    # Handle room count events separately
    if event_type == "room_count":
        people = meta.get("people")

        if type(people) is not int or people < 0:
            raise ValueError(
                "room_count requires a non-negative integer people"
            )

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO room_counts (camera_id, ts, people)
                    VALUES (%s, %s, %s)
                    """,
                    (camera_id, timestamp, people),
                )

        return {"status": "saved", "event_type": event_type}

    # Handle table events
    table_id = (
        meta.get("table_id")
        if event_type.startswith("table_")
        else None
    )

    if event_type.startswith("table_") and not table_id:
        raise ValueError("Table events require meta.table_id")

    if event_type == "table_occupied":
        people = meta.get("people", 0)

        if type(people) is not int or people < 0:
            raise ValueError(
                "table_occupied requires a non-negative integer people"
            )

    with get_connection() as conn:
        with conn.cursor() as cur:

            if event_type.startswith("table_"):

                # Check that the table exists
                cur.execute(
                    """
                    SELECT id
                    FROM restaurant_tables
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (table_id,),
                )

                if cur.fetchone() is None:
                    # Auto-register table if not previously defined so events are never dropped
                    cur.execute(
                        """
                        INSERT INTO restaurant_tables
                            (id, camera_id, polygon, source, active, updated_at)
                        VALUES (%s, %s, '[]'::jsonb, 'auto_event', TRUE, NOW())
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (table_id, camera_id),
                    )

                # Get current table status
                cur.execute(
                    """
                    SELECT status
                    FROM table_status
                    WHERE table_id = %s
                    FOR UPDATE
                    """,
                    (table_id,),
                )

                status_row = cur.fetchone()
                current_status = (
                    status_row[0] if status_row else "free"
                )

                # Ignore repeated occupied events
                if (
                    event_type == "table_occupied"
                    and current_status == "occupied"
                ):
                    return {
                        "status": "ignored",
                        "reason": "already occupied",
                    }

                # Ignore free events without an open session
                if (
                    event_type == "table_free"
                    and current_status != "occupied"
                ):
                    return {
                        "status": "ignored",
                        "reason": "no open session",
                    }

            # Insert event and prevent duplicates
            cur.execute(
                """
                INSERT INTO events
                    (camera_id, track_id, event_type, ts, table_id, meta)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    camera_id,
                    track_id,
                    event_type,
                    timestamp,
                    table_id,
                    Jsonb(meta),
                ),
            )

            inserted = cur.fetchone()

            if inserted is None:
                return {"status": "duplicate"}

            # Handle entry and exit events
            if event_type in ("enter", "exit"):

                entered = 1 if event_type == "enter" else 0
                exited = 1 if event_type == "exit" else 0

                # Calculate occupancy using events up to this timestamp
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (
                            WHERE event_type = 'enter'
                        ),
                        COUNT(*) FILTER (
                            WHERE event_type = 'exit'
                        )
                    FROM events
                    WHERE event_type IN ('enter', 'exit')
                      AND ts <= %s
                    """,
                    (timestamp,),
                )

                total_entered, total_exited = cur.fetchone()
                current = max(total_entered - total_exited, 0)

                # Update daily statistics
                cur.execute(
                    """
                    INSERT INTO daily_stats
                        (day, total_entered, total_exited, peak_occupancy)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (day)
                    DO UPDATE SET
                        total_entered =
                            daily_stats.total_entered
                            + EXCLUDED.total_entered,

                        total_exited =
                            daily_stats.total_exited
                            + EXCLUDED.total_exited,

                        peak_occupancy = GREATEST(
                            daily_stats.peak_occupancy,
                            EXCLUDED.peak_occupancy
                        )
                    """,
                    (
                        timestamp.date(),
                        entered,
                        exited,
                        current,
                    ),
                )

            # Handle table occupied events
            elif event_type == "table_occupied":

                cur.execute(
                    """
                    INSERT INTO table_status
                        (table_id, status, since, people, updated_at)
                    VALUES (%s, 'occupied', %s, %s, %s)
                    ON CONFLICT (table_id)
                    DO UPDATE SET
                        status = 'occupied',
                        since = EXCLUDED.since,
                        people = EXCLUDED.people,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        table_id,
                        timestamp,
                        people,
                        timestamp,
                    ),
                )

                # Start a new table session
                cur.execute(
                    """
                    INSERT INTO table_sessions
                        (table_id, started_at, max_people)
                    VALUES (%s, %s, %s)
                    """,
                    (table_id, timestamp, people),
                )

            # Handle table free events
            elif event_type == "table_free":

                cur.execute(
                    """
                    UPDATE table_sessions
                    SET ended_at = %s
                    WHERE table_id = %s
                      AND ended_at IS NULL
                    """,
                    (timestamp, table_id),
                )

                cur.execute(
                    """
                    UPDATE table_status
                    SET status = 'free',
                        since = %s,
                        people = 0,
                        updated_at = %s
                    WHERE table_id = %s
                    """,
                    (timestamp, timestamp, table_id),
                )

    return {"status": "saved", "event_type": event_type}


def upsert_tables(tables, camera_id="wide", source="automatic"):
    """Insert or update restaurant table polygons."""

    with get_connection() as conn:
        with conn.cursor() as cur:
            for table_id, polygon in tables.items():
                cur.execute(
                    """
                    INSERT INTO restaurant_tables
                        (id, camera_id, polygon, source, active, updated_at)
                    VALUES (%s, %s, %s, %s, TRUE, NOW())
                    ON CONFLICT (id)
                    DO UPDATE SET
                        camera_id = EXCLUDED.camera_id,
                        polygon = EXCLUDED.polygon,
                        source = EXCLUDED.source,
                        active = TRUE,
                        updated_at = NOW()
                    """,
                    (
                        table_id,
                        camera_id,
                        Jsonb(polygon),
                        source,
                    ),
                )


def get_tables():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, camera_id, polygon, source, active, updated_at
                FROM restaurant_tables
                ORDER BY id
                """
            )

            return [
                {
                    "id": row[0],
                    "camera_id": row[1],
                    "polygon": row[2],
                    "source": row[3],
                    "active": row[4],
                    "updated_at": row[5].isoformat() if row[5] else None,
                }
                for row in cur.fetchall()
            ]


def get_table_status():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_id, status, since, people, updated_at
                FROM table_status
                ORDER BY table_id
                """
            )

            return [
                {
                    "table_id": row[0],
                    "status": row[1],
                    "since": row[2].isoformat() if row[2] else None,
                    "people": row[3],
                    "updated_at": row[4].isoformat() if row[4] else None,
                }
                for row in cur.fetchall()
            ]


def get_table_sessions(from_date=None, to_date=None, table_id=None):
    """Get sessions that overlap the requested time range."""

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, table_id, started_at, ended_at, max_people
                FROM table_sessions
                WHERE (
                    %s::timestamptz IS NULL
                    OR COALESCE(ended_at, 'infinity'::timestamptz) >= %s
                )
                AND (
                    %s::timestamptz IS NULL
                    OR started_at <= %s
                )
                AND (
                    %s::text IS NULL
                    OR table_id = %s
                )
                ORDER BY started_at DESC
                """,
                (
                    from_date,
                    from_date,
                    to_date,
                    to_date,
                    table_id,
                    table_id,
                ),
            )

            return [
                {
                    "id": row[0],
                    "table_id": row[1],
                    "started_at": row[2].isoformat(),
                    "ended_at": row[3].isoformat() if row[3] else None,
                    "max_people": row[4],
                }
                for row in cur.fetchall()
            ]


def get_room_counts(from_date=None, to_date=None):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, camera_id, ts, people
                FROM room_counts
                WHERE (%s::timestamptz IS NULL OR ts >= %s)
                  AND (%s::timestamptz IS NULL OR ts <= %s)
                ORDER BY ts DESC
                """,
                (
                    from_date,
                    from_date,
                    to_date,
                    to_date,
                ),
            )

            return [
                {
                    "id": row[0],
                    "camera_id": row[1],
                    "timestamp": row[2].isoformat(),
                    "people": row[3],
                }
                for row in cur.fetchall()
            ]


def get_occupancy():
    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE event_type = 'enter'
                    ),
                    COUNT(*) FILTER (
                        WHERE event_type = 'exit'
                    )
                FROM events
                WHERE event_type IN ('enter', 'exit')
                """
            )

            entered, exited = cur.fetchone()

            cur.execute(
                """
                SELECT people
                FROM room_counts
                ORDER BY ts DESC
                LIMIT 1
                """
            )

            row = cur.fetchone()
            in_room = row[0] if row else 0

            return {
                "entered": entered,
                "exited": exited,
                "current": max(entered - exited, 0),
                "in_room": in_room,
            }


def get_occupancy_stats():
    occupancy = get_occupancy()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(ts) FROM events"
            )

            updated_at = cur.fetchone()[0]

    return {
        **occupancy,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


def get_events(from_date=None, to_date=None, camera_id=None):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, camera_id, track_id, event_type, ts, table_id, meta
                FROM events
                WHERE (%s::timestamptz IS NULL OR ts >= %s)
                  AND (%s::timestamptz IS NULL OR ts <= %s)
                  AND (%s::text IS NULL OR camera_id = %s)
                ORDER BY ts DESC
                """,
                (
                    from_date,
                    from_date,
                    to_date,
                    to_date,
                    camera_id,
                    camera_id,
                ),
            )

            return [
                {
                    "id": row[0],
                    "camera_id": row[1],
                    "track_id": row[2],
                    "event_type": row[3],
                    "timestamp": row[4].isoformat(),
                    "table_id": row[5],
                    "meta": row[6],
                }
                for row in cur.fetchall()
            ]

