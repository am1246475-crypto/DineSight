import { config } from "../config";
import type { ConnectionStatus, CountEvent, EventsQuery, LiveMessage, Occupancy } from "../types";
import { normalizeEvents } from "../lib/normalize";
import { mockEvents, mockLive, mockOccupancy } from "./mock";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function http<T>(path: string, params?: URLSearchParams): Promise<T> {
  const qs = params && params.toString() ? `?${params}` : "";
  const res = await fetch(`${config.apiUrl}${path}${qs}`, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
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
  if (!Array.isArray(raw)) throw new ApiError(500, "Unexpected /events response (not a list)");
  const { events, dropped } = normalizeEvents(raw);
  if (dropped) console.warn(`[api] dropped ${dropped} malformed event(s) from /events`);
  return events;
};

/** Single entry point for live data (mock, or WebSocket with exponential backoff). */
export function connectLive(
  onMsg: (m: LiveMessage) => void,
  onStatus: (s: ConnectionStatus) => void,
): () => void {
  if (config.useMock) {
    onStatus("live");
    return mockLive(onMsg);
  }
  let ws: WebSocket | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let attempt = 0;
  let closed = false;

  const open = () => {
    onStatus("connecting");
    ws = new WebSocket(`${config.apiUrl.replace(/^http/, "ws")}/ws/live`);
    ws.onopen = () => { attempt = 0; onStatus("live"); };
    ws.onmessage = (e) => {
      try {
        const m = JSON.parse(e.data) as LiveMessage;
        if (m.type === "occupancy") onMsg(m);
      } catch { /* ignore malformed frames */ }
    };
    ws.onclose = () => {
      onStatus("offline");
      if (!closed) timer = setTimeout(open, Math.min(1000 * 2 ** attempt++, 15_000));
    };
  };
  open();
  return () => { closed = true; clearTimeout(timer); ws?.close(); };
}
