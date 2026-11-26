# src/backend/services/analytics.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import get_connection


def get_hourly_traffic(
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    camera_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Calculates hourly entry and exit traffic."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    DATE_TRUNC('hour', ts) AS hour_bucket,
                    COUNT(*) FILTER (WHERE event_type = 'enter') AS enter_count,
                    COUNT(*) FILTER (WHERE event_type = 'exit') AS exit_count
                FROM events
                WHERE event_type IN ('enter', 'exit')
                  AND (%s::timestamptz IS NULL OR ts >= %s)
                  AND (%s::timestamptz IS NULL OR ts <= %s)
                  AND (%s::text IS NULL OR camera_id = %s)
                GROUP BY hour_bucket
                ORDER BY hour_bucket ASC
                """,
                (from_date, from_date, to_date, to_date, camera_id, camera_id),
            )
            return [
                {
                    "hour": row[0].isoformat() if row[0] else None,
                    "enter": int(row[1] or 0),
                    "exit": int(row[2] or 0),
                    "net": int(row[1] or 0) - int(row[2] or 0),
                }
                for row in cur.fetchall()
            ]


def get_peak_hours(
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    camera_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Aggregates hall occupancy by hour of the day to discover busiest hours."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    EXTRACT(HOUR FROM ts)::int AS hour_of_day,
                    ROUND(AVG(people)::numeric, 1) AS avg_people,
                    MAX(people) AS max_people,
                    COUNT(*) AS samples
                FROM room_counts
                WHERE (%s::timestamptz IS NULL OR ts >= %s)
                  AND (%s::timestamptz IS NULL OR ts <= %s)
                  AND (%s::text IS NULL OR camera_id = %s)
                GROUP BY hour_of_day
                ORDER BY avg_people DESC
                """,
                (from_date, from_date, to_date, to_date, camera_id, camera_id),
            )
            return [
                {
                    "hour_of_day": int(row[0]),
                    "avg_people": float(row[1] or 0.0),
                    "max_people": int(row[2] or 0),
                    "samples": int(row[3] or 0),
                }
                for row in cur.fetchall()
            ]


def get_table_kpis(
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    table_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Calculates Average Table Occupancy Duration, Turnover Rate, and per-table stats."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Per-table summary
            cur.execute(
                """
                SELECT
                    table_id,
                    COUNT(*) AS total_sessions,
                    ROUND(AVG(EXTRACT(EPOCH FROM (COALESCE(ended_at, NOW()) - started_at)) / 60.0)::numeric, 1) AS avg_duration_minutes,
                    ROUND(MAX(EXTRACT(EPOCH FROM (COALESCE(ended_at, NOW()) - started_at)) / 60.0)::numeric, 1) AS max_duration_minutes,
                    ROUND(AVG(COALESCE(max_people, 1))::numeric, 1) AS avg_people
                FROM table_sessions
                WHERE (%s::timestamptz IS NULL OR started_at >= %s)
                  AND (%s::timestamptz IS NULL OR started_at <= %s)
                  AND (%s::text IS NULL OR table_id = %s)
                GROUP BY table_id
                ORDER BY table_id
                """,
                (from_date, from_date, to_date, to_date, table_id, table_id),
            )
            per_table = [
                {
                    "table_id": row[0],
                    "total_sessions": int(row[1]),
                    "avg_duration_minutes": float(row[2] or 0.0),
                    "max_duration_minutes": float(row[3] or 0.0),
                    "avg_people": float(row[4] or 0.0),
                }
                for row in cur.fetchall()
            ]

            # Overall averages
            cur.execute(
                """
                SELECT
                    COUNT(*) AS all_sessions,
                    ROUND(AVG(EXTRACT(EPOCH FROM (COALESCE(ended_at, NOW()) - started_at)) / 60.0)::numeric, 1) AS overall_avg_duration_minutes,
                    COUNT(DISTINCT table_id) AS active_tables,
                    ROUND(
                        (COUNT(*)::numeric / GREATEST(1.0, EXTRACT(EPOCH FROM (COALESCE(%s::timestamptz, NOW()) - COALESCE(%s::timestamptz, MIN(started_at)))) / 3600.0))::numeric,
                        2
                    ) AS overall_turnover_rate_per_hour
                FROM table_sessions
                WHERE (%s::timestamptz IS NULL OR started_at >= %s)
                  AND (%s::timestamptz IS NULL OR started_at <= %s)
                """,
                (to_date, from_date, from_date, from_date, to_date, to_date),
            )
            overall_row = cur.fetchone()
            return {
                "overall": {
                    "total_sessions": int(overall_row[0] or 0) if overall_row else 0,
                    "avg_occupancy_duration_minutes": float(overall_row[1] or 0.0) if overall_row else 0.0,
                    "active_tables": int(overall_row[2] or 0) if overall_row else 0,
                    "turnover_rate_per_hour": float(overall_row[3] or 0.0) if overall_row else 0.0,
                },
                "tables": per_table,
            }

