import { config } from "../config";
import type { CountEvent, EventsQuery, EventType, LiveMessage, Occupancy, RoomCountPoint, TableLayout, TableState } from "../types";

const state = { entered: 0, exited: 0, events: [] as CountEvent[] }; // events: newest first
const inside: number[] = [];
let seq = 0, tid = 0;

const makeEvent = (type: EventType, at: number): CountEvent => {
  let track: number;
  if (type === "enter") { track = ++tid; inside.push(track); }
  else track = inside.length ? inside.splice(Math.floor(Math.random() * inside.length), 1)[0] : ++tid;
  const { w, h } = config.frame;
  return {
    id: ++seq,
    camera_id: Math.random() < 0.85 ? "entrance" : "exit",
    track_id: track,
    event_type: type,
    timestamp: new Date(at).toISOString(),
    meta: { frame: Math.floor(at / 40) % 100000,
            anchor: [Math.round(w * config.lineX + (Math.random() - 0.5) * 30), Math.round(h * (0.35 + Math.random() * 0.6))] },
  };
};

function record(type: EventType, at: number) {
  state.events.unshift(makeEvent(type, at));
  type === "enter" ? state.entered++ : state.exited++;
}

(function seed() {
  const now = Date.now(), span = 14 * 3600_000;
  let t = now - span;
  while (t < now - 60_000) {
    t += Math.random() * (span / 150) * 2;
    const run = state.entered - state.exited;
    record(run <= 0 || Math.random() < 0.56 ? "enter" : "exit", Math.min(t, now - 30_000));
  }
})();

export const mockOccupancy = (): Occupancy => ({
  current: Math.max(state.entered - state.exited, 0), entered: state.entered, exited: state.exited,
  updated_at: state.events[0]?.timestamp ?? null,
});

export const mockEvents = ({ from, to, cameraId }: EventsQuery): CountEvent[] =>
  state.events.filter((e) =>
    (!cameraId || e.camera_id === cameraId) && (!from || e.timestamp >= from) && (!to || e.timestamp <= to));

// ---- hall camera room count (what is really visible inside; drifts away from the entrance count on purpose)
let drift = 2;
const roomNow = () => Math.max(0, mockOccupancy().current - drift);
const roomHist: RoomCountPoint[] = [];
(function seedRoom() {
  const now = Date.now();
  let v = 4;
  for (let t = now - 3 * 3600_000; t < now; t += 60_000) {
    v = Math.max(0, Math.min(16, v + Math.round((Math.random() - 0.46) * 3)));
    roomHist.push({ t, count: v });
  }
})();
export const mockRoomHistory = (): RoomCountPoint[] => roomHist.map((p) => ({ ...p }));

// ---- tables: polygons are normalized 0..1 of the 16:9 camera frame
type P = [number, number];
const rect = (cx: number, cy: number, w: number, h: number): P[] =>
  [[cx - w / 2, cy - h / 2], [cx + w / 2, cy - h / 2], [cx + w / 2, cy + h / 2], [cx - w / 2, cy + h / 2]];
const ngon = (cx: number, cy: number, r: number, n: number, rot = 0): P[] =>
  Array.from({ length: n }, (_, i) => {
    const a = rot + (i / n) * Math.PI * 2;
    return [cx + Math.cos(a) * r * (9 / 16), cy + Math.sin(a) * r] as P; // x radius is smaller: the frame is wider than tall
  });

const MOCK_TABLES: { id: string; name: string; polygon: P[]; seats: number }[] = [
  { id: "T1", name: "Table 1", polygon: ngon(0.12, 0.2, 0.1, 8), seats: 4 },
  { id: "T2", name: "Table 2", polygon: ngon(0.3, 0.2, 0.1, 8), seats: 4 },
  { id: "T3", name: "Table 3", polygon: rect(0.5, 0.2, 0.15, 0.17), seats: 4 },
  { id: "T4", name: "Table 4", polygon: rect(0.72, 0.2, 0.2, 0.16), seats: 6 },
  { id: "T5", name: "Table 5", polygon: rect(0.92, 0.22, 0.11, 0.2), seats: 2 },
  { id: "T6", name: "Table 6", polygon: rect(0.14, 0.52, 0.15, 0.17), seats: 4 },
  { id: "T7", name: "Table 7", polygon: rect(0.36, 0.52, 0.2, 0.16), seats: 6 },
  { id: "T8", name: "Table 8", polygon: ngon(0.6, 0.52, 0.11, 6, Math.PI / 6), seats: 4 },
  { id: "T9", name: "Table 9", polygon: ngon(0.82, 0.54, 0.1, 8), seats: 4 },
  { id: "T10", name: "Table 10", polygon: rect(0.2, 0.84, 0.2, 0.15), seats: 6 },
  { id: "T11", name: "Table 11", polygon: [[0.46, 0.76], [0.62, 0.78], [0.6, 0.93], [0.44, 0.92]], seats: 4 },
  { id: "T12", name: "Table 12", polygon: [[0.74, 0.77], [0.9, 0.75], [0.92, 0.92], [0.76, 0.94]], seats: 4 },
];
const tState = new Map<string, TableState>(MOCK_TABLES.map((t) => {
  const busy = Math.random() < 0.5;
  return [t.id, { id: t.id, status: busy ? "occupied" : "free", people: busy ? 1 + Math.floor(Math.random() * t.seats) : 0,
                  since: busy ? new Date(Date.now() - (1 + Math.floor(Math.random() * 80)) * 60_000).toISOString() : null }];
}));

export const mockTableLayout = (): TableLayout[] => MOCK_TABLES.map(({ id, name, polygon }) => ({ id, name, polygon }));
export const mockTableStatus = (): TableState[] => [...tState.values()].map((s) => ({ ...s }));

function flipTable(): TableState {
  const t = MOCK_TABLES[Math.floor(Math.random() * MOCK_TABLES.length)];
  const cur = tState.get(t.id)!;
  const next: TableState = cur.status === "free"
    ? { id: t.id, status: "occupied", people: 1 + Math.floor(Math.random() * t.seats), since: new Date().toISOString() }
    : { id: t.id, status: "free", people: 0, since: null };
  tState.set(t.id, next);
  return next;
}

export function mockLive(onMsg: (m: LiveMessage) => void): () => void {
  const id = setInterval(() => {
    const run = state.entered - state.exited;
    record(run <= 0 || Math.random() > 0.45 ? "enter" : "exit", Date.now());
    const o = mockOccupancy();
    onMsg({ type: "occupancy", current: o.current, entered: o.entered, exited: o.exited });
    drift = Math.max(-1, Math.min(5, drift + (Math.random() < 0.3 ? (Math.random() < 0.5 ? -1 : 1) : 0)));
    const count = roomNow();
    roomHist.push({ t: Date.now(), count });
    onMsg({ type: "room_count", count, t: Date.now() });
    if (Math.random() < 0.5) onMsg({ type: "table", tables: [flipTable()] });
  }, 3000);
  return () => clearInterval(id);
}
