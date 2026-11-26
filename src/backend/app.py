
import asyncio
import os
from datetime import datetime
from typing import Optional, Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

from db import (
    save_event,
    get_occupancy_stats,
    get_events,
    upsert_tables,
    get_tables,
    get_table_status,
    get_table_sessions,
    get_room_counts,
)
from routes.analytics import router as analytics_router
from kafka.consumer import DineSightKafkaConsumer # type: ignore

app = FastAPI(title="DineSight API")
app.include_router(analytics_router)

# Optional Kafka Consumer instance
_kafka_consumer: Optional[DineSightKafkaConsumer] = None


async def broadcast_event_data(event_data: dict):
    event_type = event_data.get("event_type")
    meta = event_data.get("meta") or {}

    if event_type in ("enter", "exit"):
        stats = get_occupancy_stats()
        await manager.broadcast({
            "type": "occupancy",
            "current": stats["current"],
            "entered": stats["entered"],
            "exited": stats["exited"],
            "in_room": stats["in_room"],
        })
    elif event_type in ("table_occupied", "table_free"):
        table_id = meta.get("table_id")
        statuses = get_table_status()
        current_status = next(
            (item for item in statuses if item["table_id"] == table_id),
            None,
        )
        await manager.broadcast({
            "type": "table",
            "table_id": table_id,
            "event_type": event_type,
            "status": current_status,
        })
    elif event_type == "room_count":
        room_counts = get_room_counts()
        latest_count = room_counts[0] if room_counts else None
        await manager.broadcast({
            "type": "room_count",
            "data": latest_count,
        })


@app.on_event("startup")
async def startup_event():
    global _kafka_consumer
    loop = asyncio.get_running_loop()

    def _sync_broadcast(ev_dict: dict):
        asyncio.run_coroutine_threadsafe(broadcast_event_data(ev_dict), loop)

    try:
        _kafka_consumer = DineSightKafkaConsumer(broadcast_callback=_sync_broadcast)
        _kafka_consumer.start_background()
        print("[FastAPI] Integrated Kafka Consumer started; real-time dashboard updates active!")
    except Exception as exc:
        print(f"[FastAPI] Note: Kafka Consumer background start skipped ({exc})")


@app.on_event("shutdown")
def shutdown_event():
    global _kafka_consumer
    if _kafka_consumer:
        _kafka_consumer.stop()



# Allow Dashboard to communicate with the backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================
# Event Schema
# =========================

class Event(BaseModel):
    camera_id: str
    track_id: Optional[int] = None

    event_type: Literal[
        "enter",
        "exit",
        "table_occupied",
        "table_free",
        "room_count"
    ]

    timestamp: Optional[datetime] = None
    meta: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_event_data(self):

        if not self.camera_id.strip():
            raise ValueError("camera_id is required")

        if self.event_type in ("table_occupied", "table_free"):
            table_id = self.meta.get("table_id")

            if not table_id:
                raise ValueError(
                    "Table events require meta.table_id"
                )

        if self.event_type == "room_count":
            people = self.meta.get("people")

            if type(people) is not int or people < 0:
                raise ValueError(
                    "room_count requires a non-negative integer people"
                )

        if self.event_type == "table_occupied":
            people = self.meta.get("people", 0)

            if type(people) is not int or people < 0:
                raise ValueError(
                    "table_occupied requires a non-negative integer people"
                )

        return self


# =========================
# Tables Schema
# =========================

class TablesUpdate(BaseModel):
    camera_id: str = "wide"
    source: str = "automatic"

    # Each table has an ID and a polygon made of coordinate pairs
    tables: dict[str, list[list[float]]] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_tables(self):

        if not self.camera_id.strip():
            raise ValueError("camera_id is required")

        for table_id, polygon in self.tables.items():

            if not table_id.strip():
                raise ValueError("table ID cannot be empty")

            if len(polygon) < 3:
                raise ValueError(
                    f"Table {table_id} polygon must have at least 3 points"
                )

            for point in polygon:
                if len(point) != 2:
                    raise ValueError(
                        f"Each point in table {table_id} must contain x and y"
                    )

        return self


# =========================
# WebSocket Connection Manager
# =========================

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


# =========================
# Health Endpoint
# =========================

@app.get("/health")
def health():
    return {"status": "ok"}


# =========================
# Events Endpoint
# =========================

@app.post("/events")
async def create_event(event: Event):

    try:
        event_data = event.model_dump(exclude_none=True)

        result = save_event(event_data)

        # Do not broadcast duplicate or ignored events
        if result.get("status") in ("duplicate", "ignored"):
            return {
                "message": "Event was not processed",
                **result
            }

        # Broadcast live update to WebSockets
        await broadcast_event_data(event_data)

        return {
            "message": "Event saved successfully",
            **result
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =========================
# Tables Endpoints
# =========================

@app.put("/tables")
def update_tables(data: TablesUpdate):

    try:
        upsert_tables(
            tables=data.tables,
            camera_id=data.camera_id,
            source=data.source,
        )

        saved_tables = get_tables()

        return {
            "message": "Tables updated successfully",
            "count": len(data.tables),
            "tables": saved_tables,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tables")
def read_tables():

    try:
        return get_tables()

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tables/status")
def read_table_status():

    try:
        return get_table_status()

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tables/history")
def read_table_history(
    from_date: Optional[datetime] = Query(default=None, alias="from"),
    to_date: Optional[datetime] = Query(default=None, alias="to"),
    table_id: Optional[str] = None,
):

    try:
        return get_table_sessions(
            from_date=from_date,
            to_date=to_date,
            table_id=table_id,
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =========================
# Occupancy Endpoint
# =========================

@app.get("/occupancy/current")
def occupancy():

    try:
        return get_occupancy_stats()

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =========================
# Room Count History Endpoint
# =========================

@app.get("/room-count/history")
def read_room_count_history(
    from_date: Optional[datetime] = Query(default=None, alias="from"),
    to_date: Optional[datetime] = Query(default=None, alias="to"),
):

    try:
        return get_room_counts(
            from_date=from_date,
            to_date=to_date,
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =========================
# Events History Endpoint
# =========================

@app.get("/events")
def read_events(
    from_date: Optional[datetime] = Query(default=None, alias="from"),
    to_date: Optional[datetime] = Query(default=None, alias="to"),
    camera_id: Optional[str] = None,
):

    try:
        return get_events(
            from_date=from_date,
            to_date=to_date,
            camera_id=camera_id,
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =========================
# WebSocket Live Endpoint
# =========================

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
            "in_room": stats["in_room"],
        })

        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        manager.disconnect(websocket)

    except Exception:
        manager.disconnect(websocket)


# =========================
# Kafka Metrics Endpoint
# =========================

@app.get("/kafka/metrics")
def kafka_metrics():
    global _kafka_consumer
    if _kafka_consumer:
        return _kafka_consumer.get_metrics()
    return {
        "status": "idle",
        "message": "Kafka consumer not currently running inside API process (start via CLI or set KAFKA_ENABLE_CONSUMER=1)",
        "bootstrap_servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
        "topic": os.getenv("KAFKA_TOPIC_EVENTS", "dinesight.events.raw"),
    }

