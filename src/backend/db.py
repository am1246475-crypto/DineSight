
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb


# Database connection settings
DB_CONFIG = {
    "dbname": "dinesight",
    "user": "postgres",
    "password": "salma",
    "host": "127.0.0.1",
    "port": 5432,
}


def get_connection():
    """Connect Python to PostgreSQL."""
    return psycopg.connect(**DB_CONFIG)


def save_event(event):
    """Save an entry/exit event and update daily statistics."""

    timestamp = event.get("timestamp") or event.get("ts")

    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")
        )

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    event_type = event["event_type"]

    if event_type not in ("enter", "exit"):
        raise ValueError("event_type must be 'enter' or 'exit'")

    with get_connection() as conn:
        with conn.cursor() as cur:

            # Save event
            cur.execute(
                """
                INSERT INTO events
                    (camera_id, track_id, event_type, ts, meta)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    event["camera_id"],
                    event["track_id"],
                    event_type,
                    timestamp,
                    Jsonb(event.get("meta", {})),
                ),
            )

            # Calculate current occupancy after saving the event
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE event_type = 'enter'),
                    COUNT(*) FILTER (WHERE event_type = 'exit')
                FROM events
                """
            )

            entered, exited = cur.fetchone()
            current = max(entered - exited, 0)

            # Update daily statistics
            cur.execute(
                """
                INSERT INTO daily_stats
                    (day, total_entered, total_exited, peak_occupancy)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (day)
                DO UPDATE SET
                    total_entered = daily_stats.total_entered
                        + EXCLUDED.total_entered,
                    total_exited = daily_stats.total_exited
                        + EXCLUDED.total_exited,
                    peak_occupancy = GREATEST(
                        daily_stats.peak_occupancy,
                        EXCLUDED.peak_occupancy
                    )
                """,
                (
                    timestamp.date(),
                    1 if event_type == "enter" else 0,
                    1 if event_type == "exit" else 0,
                    current,
                ),
            )


def get_occupancy():
    """Calculate current occupancy."""

    query = """
        SELECT
            COUNT(*) FILTER (WHERE event_type = 'enter'),
            COUNT(*) FILTER (WHERE event_type = 'exit')
        FROM events
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            entered, exited = cur.fetchone()

            return max(entered - exited, 0)


def get_occupancy_stats():
    """Return current occupancy, total entries, exits and last update."""

    query = """
        SELECT
            COUNT(*) FILTER (WHERE event_type = 'enter'),
            COUNT(*) FILTER (WHERE event_type = 'exit'),
            MAX(ts)
        FROM events
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            entered, exited, updated_at = cur.fetchone()

            return {
                "current": max(entered - exited, 0),
                "entered": entered,
                "exited": exited,
                "updated_at": (
                    updated_at.isoformat()
                    if updated_at
                    else None
                ),
            }


def get_events(from_date=None, to_date=None, camera_id=None):
    """Get events filtered by date range and camera."""

    query = """
        SELECT id, camera_id, track_id, event_type, ts, meta
        FROM events
        WHERE (%s::timestamptz IS NULL OR ts >= %s)
          AND (%s::timestamptz IS NULL OR ts <= %s)
          AND (%s::text IS NULL OR camera_id = %s)
        ORDER BY ts DESC
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                query,
                (
                    from_date, from_date,
                    to_date, to_date,
                    camera_id, camera_id,
                ),
            )

            rows = cur.fetchall()

            return [
                {
                    "id": row[0],
                    "camera_id": row[1],
                    "track_id": row[2],
                    "event_type": row[3],
                    "timestamp": row[4].isoformat(),
                    "meta": row[5],
                }
                for row in rows
            ]
