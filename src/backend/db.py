
import os
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb


# Database connection settings
DB_CONFIG = {
    "dbname": "dinesight",
    "user": "postgres",
    "password": "farah",
    "host": "localhost",
    "port": 5432,
}


def get_connection():
    """Connect Python to PostgreSQL."""
    return psycopg.connect(**DB_CONFIG)


def save_event(event):
    """
    Save one entry/exit event coming from the CV pipeline.
    """

    query = """
        INSERT INTO events (camera_id, track_id, event_type, ts, meta)
        VALUES (%s, %s, %s, %s, %s)
    """

    timestamp = event.get("timestamp") or event.get("ts")

    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")
        )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                query,
                (
                    event["camera_id"],
                    event["track_id"],
                    event["event_type"],
                    timestamp,
                    Jsonb(event.get("meta", {})),
                ),
            )


def get_occupancy():
    """
    Calculate current occupancy from entry and exit events.
    """

    query = """
        SELECT COALESCE(
            SUM(
                CASE
                    WHEN event_type = 'enter' THEN 1
                    WHEN event_type = 'exit' THEN -1
                    ELSE 0
                END
            ),
            0
        )
        FROM events
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            result = cur.fetchone()[0]

            return max(result, 0)


def get_events(from_date=None, to_date=None, camera_id=None):
    """
    Get events filtered by date range and camera.
    """

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
