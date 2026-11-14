// Mirrors the backend contract (src/backend/app.py + db.py)
export type EventType = "enter" | "exit";

export interface CountEvent {
  id?: number;
  camera_id: string;
  track_id: number;
  event_type: EventType;
  timestamp: string; // ISO 8601
  meta: { frame?: number; anchor?: [number, number] } & Record<string, unknown>;
}

export interface Occupancy {
  current: number;
  entered: number;
  exited: number;
  updated_at?: string | null;
}

export interface LiveMessage extends Occupancy {
  type: "occupancy";
}

export interface EventsQuery {
  from?: string;
  to?: string;
  cameraId?: string;
}

export type ConnectionStatus = "connecting" | "live" | "offline";
export type Preset = "today" | "24h" | "7d" | "all";
