
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from db import save_event, get_occupancy, get_events


app = FastAPI(title="DineSight API")


class Event(BaseModel):
    camera_id: str
    track_id: int
    event_type: str
    timestamp: Optional[datetime] = None
    meta: dict = Field(default_factory=dict)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/events")
def create_event(event: Event):
    try:
        save_event(event.model_dump(exclude_none=True))
        return {"message": "Event saved successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/occupancy")
def occupancy():
    try:
        return {"occupancy": get_occupancy()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/events")
def read_events(
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    camera_id: Optional[str] = None,
):
    try:
        return get_events(from_date, to_date, camera_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
