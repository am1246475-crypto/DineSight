import { config } from "../config";
import type { ConnectionStatus, CountEvent, EventsQuery, LiveMessage, Occupancy, RoomCountPoint, TableLayout, TableState } from "../types";
import { normalizeEvents, normalizeRoomHistory, normalizeTableLayout, normalizeTableStatus, parseLiveMessage } from "../lib/normalize";
import { mockEvents, mockLive, mockOccupancy, mockRoomHistory, mockTableLayout, mockTableStatus } from "./mock";

export class ApiError extends Error {
  constructor(public status: number, message: string, public path = "") {
    super(message);
  }
}

async function http<T>(path: string, params?: URLSearchParams): Promise<T> {
  const qs = params && params.toString() ? `?${params}` : "";
  let res: Response;
  try {
    res = await fetch(`${config.apiUrl}${path}${qs}`, { headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError(0, "Cannot reach the server", path); // network down / CORS / backend not running
  }
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${res.statusText}`, path);
  try { return (await res.json()) as T; }
  catch { throw new ApiError(res.status, "Response is not valid JSON", path); }
}

export const getOccupancy = async (): Promise<Occupancy> =>
  config.useMock ? mockOccupancy() : http<Occupancy>("/occupancy/current");

export const getEvents = async ({ from, to, cameraId }: EventsQuery = {}): Promise<CountEvent[]> => {
  if (config.useMock) return mockEvents({ from, to, cameraId });
  const p = new URLSearchParams();
  if (from) p.set("from", from);
  if (to) p.set("to", to);
  if (cameraId) p.set("camera_id", cameraId);
  const raw = await http<unknown>("/events", p);
  if (!Array.isArray(raw)) throw new ApiError(500, "Unexpected /events response (not a list)", "/events");
  const { events, dropped } = normalizeEvents(raw);
  if (dropped) console.warn(`[api] dropped ${dropped} malformed event(s) from /events`);
  return events;
};

/** GET /tables: table polygons (normalized 0..1). */
export const getTableLayout = async (): Promise<TableLayout[]> => {
  if (config.useMock) return mockTableLayout();
  const { tables, dropped } = normalizeTableLayout(await http<unknown>("/tables"));
  if (dropped) console.warn(`[api] dropped ${dropped} malformed table(s) from /tables`);
  return tables;
};

/** GET /tables/status: occupied / free, people and since when. */
export const getTableStatus = async (): Promise<TableState[]> => {
  if (config.useMock) return mockTableStatus();
  const { states, dropped } = normalizeTableStatus(await http<unknown>("/tables/status"));
  if (dropped) console.warn(`[api] dropped ${dropped} malformed row(s) from /tables/status`);
  return states;
};

/** GET /room-count/history: people count seen by the hall camera over time. */
export const getRoomHistory = async (): Promise<RoomCountPoint[]> => {
  if (config.useMock) return mockRoomHistory();
  const { points, dropped } = normalizeRoomHistory(await http<unknown>("/room-count/history"));
  if (dropped) console.warn(`[api] dropped ${dropped} malformed sample(s) from /room-count/history`);
  return points;
};

export interface LiveConnection {
  close: () => void;
  /** Skip the backoff wait and reconnect right now ("Retry now" button). */
  reconnect: () => void;
}

/**
 * Single entry point for live data: mock, or WebSocket /ws/live with automatic reconnect
 * (exponential backoff 1s, 2s, 4s ... capped at 15s, with a little jitter).
 * onRetry(attempt, delayMs) tells the UI when the next attempt is coming so it can show a countdown.
 */
export function connectLive(
  onMsg: (m: LiveMessage) => void,
  onStatus: (s: ConnectionStatus) => void,
  onRetry?: (attempt: number, delayMs: number) => void,
): LiveConnection {
  if (config.useMock) {
    onStatus("live");
    return { close: mockLive(onMsg), reconnect: () => {} };
  }
  let ws: WebSocket | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let attempt = 0;
  let closed = false;

  const detach = (s?: WebSocket) => { if (s) { s.onopen = s.onmessage = s.onclose = s.onerror = null; } };

  const open = () => {
    clearTimeout(timer);
    detach(ws);
    try { ws?.close(); } catch { /* already closed */ }
    onStatus("connecting");
    const sock = new WebSocket(`${config.apiUrl.replace(/^http/, "ws")}/ws/live`);
    ws = sock;
    sock.onopen = () => { attempt = 0; onStatus("live"); };
    sock.onmessage = (e) => {
      try {
        const m = parseLiveMessage(JSON.parse(e.data));
        if (m) onMsg(m);
      } catch { /* ignore malformed frames */ }
    };
    sock.onerror = () => { /* a close event always follows; reconnect is handled there */ };
    sock.onclose = () => {
      if (closed || ws !== sock) return;
      onStatus("offline");
      const delay = Math.min(1000 * 2 ** attempt, 15_000) + Math.random() * 400;
      attempt++;
      onRetry?.(attempt, delay);
      timer = setTimeout(open, delay);
    };
  };
  open();
  return {
    close: () => { closed = true; clearTimeout(timer); detach(ws); ws?.close(); },
    reconnect: () => { if (!closed) { attempt = 0; open(); } },
  };
}
