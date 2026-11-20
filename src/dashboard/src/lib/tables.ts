import type { RoomCountPoint, TableLayout, TableState, TableView } from "../types";

/** "less than a minute", "1 minute", "12 minutes", "1 hour", "1 hour 5 minutes", "2 hours 30 minutes". */
export function fmtDuration(ms: number): string {
  const m = Math.floor(Math.max(0, ms) / 60_000);
  if (m < 1) return "less than a minute";
  if (m < 60) return `${m} minute${m === 1 ? "" : "s"}`;
  const h = Math.floor(m / 60), r = m % 60;
  return `${h} hour${h === 1 ? "" : "s"}${r ? ` ${r} minute${r === 1 ? "" : "s"}` : ""}`;
}

export const sinceMs = (since: string | null, now: number): number | null => {
  if (!since) return null;
  const t = Date.parse(since);
  return Number.isNaN(t) ? null : Math.max(0, now - t);
};

/** polygon "x,y x,y ..." scaled from 0..1 to the SVG viewBox. */
export const toPoints = (poly: [number, number][], w: number, h: number) => poly.map(([x, y]) => `${+(x * w).toFixed(2)},${+(y * h).toFixed(2)}`).join(" ");

export function bbox(poly: [number, number][]) {
  const xs = poly.map((p) => p[0]), ys = poly.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  return { x0, x1, y0, y1, w: x1 - x0, h: y1 - y0 };
}

/** Area-weighted centroid (falls back to the bounding-box centre for degenerate polygons). */
export function centroid(poly: [number, number][]): [number, number] {
  let a = 0, cx = 0, cy = 0;
  for (let i = 0; i < poly.length; i++) {
    const [x0, y0] = poly[i], [x1, y1] = poly[(i + 1) % poly.length];
    const k = x0 * y1 - x1 * y0;
    a += k; cx += (x0 + x1) * k; cy += (y0 + y1) * k;
  }
  if (Math.abs(a) < 1e-9) { const b = bbox(poly); return [(b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2]; }
  return [cx / (3 * a), cy / (3 * a)];
}

/**
 * Joins the layout with the live states. A table the status endpoint does not list is "free" once the status
 * loaded (backends often return only the busy ones) and "unknown" while there is no status at all.
 */
export function mergeTables(layout: TableLayout[], states: TableState[] | undefined): TableView[] {
  const byId = new Map((states ?? []).map((s) => [s.id, s]));
  return layout.map((t) => {
    const s = byId.get(t.id);
    if (s) return { ...t, status: s.status, people: s.people, since: s.since };
    return { ...t, status: states ? "free" : "unknown", people: 0, since: null };
  });
}

/** Applies a WebSocket "table" message on top of the cached states (unknown ids are added so they are not lost). */
export function patchStates(prev: TableState[] | undefined, incoming: TableState[]): TableState[] {
  const map = new Map((prev ?? []).map((s) => [s.id, s]));
  for (const s of incoming) map.set(s.id, s);
  return [...map.values()];
}

/** Appends one live sample, ignoring out-of-order ones, and caps the series length. */
export function appendPoint(prev: RoomCountPoint[] | undefined, p: RoomCountPoint, cap = 5000): RoomCountPoint[] {
  const base = prev ?? [];
  const last = base[base.length - 1];
  if (last && p.t < last.t) return base;
  const next = last && last.t === p.t ? [...base.slice(0, -1), p] : [...base, p];
  return next.length > cap ? next.slice(next.length - cap) : next;
}

/** Natural order: T2 before T10. */
export const byName = (a: { id: string }, b: { id: string }) => a.id.localeCompare(b.id, undefined, { numeric: true });

/** Difference between the entrance-computed count and what the hall camera sees. */
export function discrepancy(entrance: number | undefined, hall: number | undefined) {
  if (entrance === undefined || hall === undefined) return null;
  const diff = entrance - hall;
  return { diff, level: diff === 0 ? "match" : Math.abs(diff) <= 2 ? "minor" : "major" } as const;
}

export interface Alert { id: string; level: "warn" | "bad"; title: string; detail: string }

/** Average and longest time the currently seated tables have been occupied. */
export function tableStats(tables: TableView[], now: number) {
  const seated = tables
    .filter((t) => t.status === "occupied")
    .map((t) => ({ name: t.name, ms: sinceMs(t.since, now) }))
    .filter((x): x is { name: string; ms: number } => x.ms !== null);
  if (!seated.length) return { avg: null, longest: null };
  const avg = seated.reduce((s, x) => s + x.ms, 0) / seated.length;
  const longest = seated.reduce((a, b) => (b.ms > a.ms ? b : a));
  return { avg, longest };
}

/** Things the manager should look at now, most urgent first. Derived only from data the dashboard already has. */
export function buildAlerts(i: { tables: TableView[]; now: number; longMin: number; entrance?: number; hall?: number; hallAt?: number }): Alert[] {
  const out: Alert[] = [];
  for (const t of i.tables) {
    const d = sinceMs(t.since, i.now);
    if (t.status === "occupied" && d !== null && d >= i.longMin * 60_000)
      out.push({ id: `long-${t.id}`, level: "warn", title: `${t.name} has been occupied for ${fmtDuration(d)}`, detail: `Longer than your ${i.longMin} minute limit.` });
  }
  const gap = discrepancy(i.entrance, i.hall);
  if (gap && gap.level === "major")
    out.push({ id: "gap", level: "warn", title: `Entrance and hall counts differ by ${Math.abs(gap.diff)}`, detail: gap.diff > 0 ? "People may be outside the hall camera view, or an exit was missed." : "An entry may have been missed at the entrance." });
  if (i.hallAt && i.now - i.hallAt > 90_000)
    out.push({ id: "stale", level: "bad", title: "Hall camera has stopped reporting", detail: `Last count was ${fmtDuration(i.now - i.hallAt)} ago. Check the camera and the CV process.` });
  return out.sort((a, b) => (a.level === b.level ? 0 : a.level === "bad" ? -1 : 1));
}

const csvCell = (v: string | number) => `"${String(v).replace(/"/g, '""')}"`;
/** Current state of every table as CSV (for the shift report). */
export function tablesToCsv(tables: TableView[], now: number): string {
  const rows = tables.map((t) => {
    const d = sinceMs(t.since, now);
    return [t.id, t.name, t.status, t.people, t.since ?? "", d === null ? "" : Math.floor(d / 60_000)].map(csvCell).join(",");
  });
  return ["table_id,name,status,people,occupied_since,minutes_occupied", ...rows].join("\n");
}
