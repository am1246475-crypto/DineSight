# src/backend/routes/analytics.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query

from services.analytics import (
    get_hourly_traffic,
    get_peak_hours,
    get_table_kpis,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/hourly-traffic")
def read_hourly_traffic(
    from_date: Optional[datetime] = Query(default=None, alias="from"),
    to_date: Optional[datetime] = Query(default=None, alias="to"),
    camera_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Retrieve hourly entry/exit traffic metrics."""
    try:
        return get_hourly_traffic(from_date=from_date, to_date=to_date, camera_id=camera_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/peak-hours")
def read_peak_hours(
    from_date: Optional[datetime] = Query(default=None, alias="from"),
    to_date: Optional[datetime] = Query(default=None, alias="to"),
    camera_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Retrieve peak hours analysis based on room occupancy readings."""
    try:
        return get_peak_hours(from_date=from_date, to_date=to_date, camera_id=camera_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/table-kpis")
def read_table_kpis(
    from_date: Optional[datetime] = Query(default=None, alias="from"),
    to_date: Optional[datetime] = Query(default=None, alias="to"),
    table_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Retrieve Average Occupancy Duration, Table Turnover Rate, and session metrics."""
    try:
        return get_table_kpis(from_date=from_date, to_date=to_date, table_id=table_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

