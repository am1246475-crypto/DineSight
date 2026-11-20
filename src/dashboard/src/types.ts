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

/** WebSocket /ws/live: three message types (all normalized in lib/normalize.ts before they reach the UI). */
export interface OccupancyMessage extends Occupancy { type: "occupancy" }
/** People currently visible by the hall camera (the "real" count, as opposed to the entrance-computed one). */
export interface RoomCountMessage { type: "room_count"; count: number; t: number /* epoch ms */ }
export interface TableMessage { type: "table"; tables: TableState[] }
export type LiveMessage = OccupancyMessage | RoomCountMessage | TableMessage;

export interface EventsQuery {
  from?: string;
  to?: string;
  cameraId?: string;
}

export type ConnectionStatus = "connecting" | "live" | "offline";
export type Preset = "today" | "24h" | "7d" | "all";

/** GET /tables: the geometry of one table. polygon points are normalized 0..1 (x right, y down) of the camera frame. */
export interface TableLayout {
  id: string;
  name: string;
  polygon: [number, number][];
}

/** GET /tables/status (and WS "table"): the live state of one table. */
export interface TableState {
  id: string;
  status: "free" | "occupied";
  people: number;
  since: string | null; // ISO time the table became occupied
}

/** Layout + state merged for rendering. "unknown" = layout loaded but the status endpoint failed. */
export interface TableView extends TableLayout {
  status: "free" | "occupied" | "unknown";
  people: number;
  since: string | null;
}

/** One sample of the hall-camera people count. */
export interface RoomCountPoint { t: number; count: number }
