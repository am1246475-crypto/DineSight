import type { CountEvent } from "../types";

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
