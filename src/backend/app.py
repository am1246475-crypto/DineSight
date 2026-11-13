
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from db import save_event, get_occupancy_stats, get_events


app = FastAPI(title="DineSight API")


# Allow Dashboard to communicate with the backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Event(BaseModel):
    camera_id: str
    track_id: int
    event_type: str
    timestamp: Optional[datetime] = None
    meta: dict = Field(default_factory=dict)


# Manage active WebSocket connections
class ConnectionManager:

    def __init__(self):
        self.active_connections = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, data: dict):
        for connection in self.active_connections.copy():
            try:
                await connection.send_json(data)
            except Exception:
                self.disconnect(connection)


manager = ConnectionManager()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/events")
async def create_event(event: Event):
    try:
        save_event(event.model_dump(exclude_none=True))

        stats = get_occupancy_stats()

        # Send occupancy update to all connected clients
        await manager.broadcast({
            "type": "occupancy",
            "current": stats["current"],
            "entered": stats["entered"],
            "exited": stats["exited"],
        })

        return {"message": "Event saved successfully"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/occupancy/current")
def occupancy():
    try:
        return get_occupancy_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/events")
def read_events(
    from_date: Optional[datetime] = Query(default=None, alias="from"),
    to_date: Optional[datetime] = Query(default=None, alias="to"),
    camera_id: Optional[str] = None,
):
    try:
        return get_events(from_date, to_date, camera_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket):
    await manager.connect(websocket)

    try:
        # Send current occupancy immediately after connection
        stats = get_occupancy_stats()

        await websocket.send_json({
            "type": "occupancy",
            "current": stats["current"],
            "entered": stats["entered"],
            "exited": stats["exited"],
        })

        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        manager.disconnect(websocket)

    except Exception:
        manager.disconnect(websocket)
