import type { CountEvent, LiveMessage, RoomCountPoint, TableLayout, TableState } from "../types";

/** Validates the /events payload. Accepts `ts` as an alias of `timestamp` (db.py accepts both on write). */
export function normalizeEvents(raw: unknown): { events: CountEvent[]; dropped: number } {
  const arr = Array.isArray(raw) ? (raw as Record<string, unknown>[]) : [];
  const events: CountEvent[] = [];
  for (const r of arr) {
    const timestamp = r?.timestamp ?? r?.ts;
    const type = r?.event_type;
    if (typeof r?.camera_id !== "string" || (type !== "enter" && type !== "exit")
        || typeof timestamp !== "string" || Number.isNaN(Date.parse(timestamp))) continue;
    events.push({
      id: typeof r.id === "number" ? r.id : undefined,
      camera_id: r.camera_id, track_id: Number(r.track_id), event_type: type, timestamp,
      meta: (r.meta && typeof r.meta === "object" ? r.meta : {}) as CountEvent["meta"],
    });
  }
  return { events, dropped: arr.length - events.length };
}

// ============================================================ tables / room count / live messages
// The backend contract for these endpoints is not final yet, so every parser accepts the common aliases
// and silently drops malformed rows (the count of dropped rows is returned so callers can warn).

const num = (v: unknown): number | undefined => {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v))) return Number(v);
  return undefined;
};
const str = (v: unknown): string | undefined => (typeof v === "string" && v !== "" ? v : typeof v === "number" ? String(v) : undefined);
const rec = (v: unknown): Record<string, unknown> => (v && typeof v === "object" ? (v as Record<string, unknown>) : {});
const clamp01 = (n: number) => Math.min(1, Math.max(0, n));
/** Accepts a bare array or an object wrapping it: {tables: [...]}, {items: [...]}, ... */
const list = (raw: unknown, ...keys: string[]): unknown[] => {
  if (Array.isArray(raw)) return raw;
  const o = rec(raw);
  for (const k of keys) if (Array.isArray(o[k])) return o[k] as unknown[];
  return [];
};
const toMs = (v: unknown): number | undefined => {
  if (typeof v === "number" && Number.isFinite(v)) return v < 1e12 ? v * 1000 : v; // seconds or milliseconds
  if (typeof v === "string") { const t = Date.parse(v); return Number.isNaN(t) ? undefined : t; }
  return undefined;
};

/** [[x,y],...] or [{x,y},...] -> [[x,y],...] clamped to 0..1. Returns null if it is not a polygon (< 3 valid points). */
export function parsePolygon(raw: unknown): [number, number][] | null {
  if (!Array.isArray(raw)) return null;
  const pts: [number, number][] = [];
  for (const p of raw) {
    const x = Array.isArray(p) ? num(p[0]) : num(rec(p).x);
    const y = Array.isArray(p) ? num(p[1]) : num(rec(p).y);
    if (x === undefined || y === undefined) return null;
    pts.push([clamp01(x), clamp01(y)]);
  }
  return pts.length >= 3 ? pts : null;
}

export function normalizeTableLayout(raw: unknown): { tables: TableLayout[]; dropped: number } {
  const arr = list(raw, "tables", "items", "data");
  const tables: TableLayout[] = [];
  for (const item of arr) {
    const r = rec(item);
    const id = str(r.id) ?? str(r.table_id) ?? str(r.name);
    const polygon = parsePolygon(r.polygon ?? r.points ?? r.coords ?? r.coordinates);
    if (!id || !polygon) continue;
    tables.push({ id, name: str(r.name) ?? str(r.label) ?? id, polygon });
  }
  return { tables, dropped: arr.length - tables.length };
}

/** One table state. `receivedAt` converts a relative duration (duration_seconds / duration_minutes) into an absolute `since`. */
export function normalizeTableState(raw: unknown, receivedAt = Date.now()): TableState | null {
  const r = rec(raw);
  const id = str(r.table_id) ?? str(r.id) ?? str(r.name);
  if (!id) return null;
  const s = typeof r.status === "string" ? r.status.toLowerCase() : "";
  const occupied = typeof r.occupied === "boolean" ? r.occupied : ["occupied", "busy", "taken", "seated"].includes(s);
  const people = Math.max(0, Math.round(num(r.people_count) ?? num(r.people) ?? num(r.person_count) ?? num(r.count) ?? 0));
  let since: string | null = null;
  if (occupied) {
    const abs = toMs(r.since ?? r.occupied_since ?? r.occupied_at ?? r.started_at);
    const rel = num(r.duration_seconds) ?? (num(r.duration_minutes) !== undefined ? num(r.duration_minutes)! * 60 : undefined) ?? num(r.duration);
    if (abs !== undefined) since = new Date(abs).toISOString();
    else if (rel !== undefined) since = new Date(receivedAt - rel * 1000).toISOString();
  }
  return { id, status: occupied ? "occupied" : "free", people, since };
}

export function normalizeTableStatus(raw: unknown): { states: TableState[]; dropped: number } {
  const arr = list(raw, "tables", "status", "items", "data");
  const now = Date.now();
  const states = arr.map((r) => normalizeTableState(r, now)).filter((s): s is TableState => s !== null);
  return { states, dropped: arr.length - states.length };
}

/** GET /room-count/history -> samples sorted by time. Accepts {timestamp,count}, {ts,room_count}, [time,count] ... */
export function normalizeRoomHistory(raw: unknown): { points: RoomCountPoint[]; dropped: number } {
  const arr = list(raw, "history", "points", "items", "data");
  const points: RoomCountPoint[] = [];
  for (const item of arr) {
    const r = rec(item);
    const t = Array.isArray(item) ? toMs(item[0]) : toMs(r.timestamp ?? r.ts ?? r.time ?? r.t);
    const count = Array.isArray(item) ? num(item[1]) : num(r.count) ?? num(r.room_count) ?? num(r.people) ?? num(r.value);
    if (t === undefined || count === undefined) continue;
    points.push({ t, count: Math.max(0, Math.round(count)) });
  }
  points.sort((a, b) => a.t - b.t);
  return { points, dropped: arr.length - points.length };
}

/** One raw WebSocket frame -> a typed message, or null for anything unknown / malformed. */
export function parseLiveMessage(raw: unknown, receivedAt = Date.now()): LiveMessage | null {
  const r = rec(raw);
  switch (r.type) {
    case "occupancy": {
      const current = num(r.current), entered = num(r.entered), exited = num(r.exited);
      return current === undefined || entered === undefined || exited === undefined ? null
        : { type: "occupancy", current, entered, exited };
    }
    case "room_count": {
      // backend sends {type, data: {people, timestamp, ...}}; also accept the flat shape
      const d = r.data && typeof r.data === "object" ? rec(r.data) : r;
      const count = num(d.count) ?? num(d.room_count) ?? num(d.people);
      return count === undefined ? null
        : { type: "room_count", count: Math.max(0, Math.round(count)), t: toMs(d.timestamp ?? d.ts ?? d.time) ?? receivedAt };
    }
    case "table": {
      // a single table ({table_id, status, ...}) or a batch ({tables: [...]})
      // backend sends {type, table_id, event_type, status: {table_id, status, since, people}}
      const inner = r.status && typeof r.status === "object" ? rec(r.status) : null;
      const many = Array.isArray(r.tables) ? r.tables : [inner ? { table_id: r.table_id, ...inner } : r];
      const tables = many.map((t) => normalizeTableState(t, receivedAt)).filter((s): s is TableState => s !== null);
      return tables.length ? { type: "table", tables } : null;
    }
    default:
      return null;
  }
}
