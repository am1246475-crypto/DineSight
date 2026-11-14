import type { CountEvent, EventType } from "../types";

export interface Analytics {
  series: { t: number; v: number }[];
  peak: number;
  min: number;
  hourly: { hour: string; enter: number; exit: number }[];
  cameras: { id: string; enter: number; exit: number }[];
  crossings: { x: number; y: number; type: EventType }[];
  dwell: { avgMin: number | null; medianMin: number | null; n: number };
  flicker: { track: number; count: number }[];
}

const ms = (e: CountEvent) => new Date(e.timestamp).getTime();

/** Derives everything from the events of the selected range. `current` anchors the occupancy curve. */
export function analyze(events: CountEvent[], current: number): Analytics {
  const asc = [...events].sort((a, b) => ms(a) - ms(b));
  const net = asc.reduce((s, e) => s + (e.event_type === "enter" ? 1 : -1), 0);

  let run = current - net; // occupancy right before the first event of the range
  let peak = run, min = run;
  const series: Analytics["series"] = asc.length ? [{ t: ms(asc[0]) - 1, v: run }] : [];
  for (const e of asc) {
    run += e.event_type === "enter" ? 1 : -1;
    peak = Math.max(peak, run); min = Math.min(min, run);
    series.push({ t: ms(e), v: run });
  }
  if (asc.length) series.push({ t: Date.now(), v: run });

  const hourly = Array.from({ length: 24 }, (_, h) => ({ hour: `${String(h).padStart(2, "0")}:00`, enter: 0, exit: 0 }));
  const cams = new Map<string, { enter: number; exit: number }>();
  const crossings: Analytics["crossings"] = [];
  const counts = new Map<number, number>();
  const openAt = new Map<number, number>();
  const stays: number[] = [];

  for (const e of asc) {
    hourly[new Date(ms(e)).getHours()][e.event_type]++;
    const c = cams.get(e.camera_id) ?? { enter: 0, exit: 0 };
    c[e.event_type]++; cams.set(e.camera_id, c);
    const a = e.meta?.anchor;
    if (Array.isArray(a)) crossings.push({ x: a[0], y: a[1], type: e.event_type });
    counts.set(e.track_id, (counts.get(e.track_id) ?? 0) + 1);
    if (e.event_type === "enter") openAt.set(e.track_id, ms(e));
    else if (openAt.has(e.track_id)) {
      const d = (ms(e) - openAt.get(e.track_id)!) / 60000;
      openAt.delete(e.track_id);
      if (d > 0 && d < 360) stays.push(d);
    }
  }
  stays.sort((a, b) => a - b);
  const avg = stays.length ? stays.reduce((s, v) => s + v, 0) / stays.length : null;
  const median = stays.length ? stays[Math.floor(stays.length / 2)] : null;

  return {
    series, peak, min, hourly, crossings,
    cameras: [...cams].map(([id, v]) => ({ id, ...v })),
    dwell: { avgMin: avg, medianMin: median, n: stays.length },
    flicker: [...counts].filter(([, n]) => n >= 4).map(([track, count]) => ({ track, count })).sort((a, b) => b.count - a.count),
  };
}

export const fmtMin = (m: number | null) =>
  m === null ? "–" : m < 60 ? `${Math.round(m)} د` : `${Math.floor(m / 60)} س ${Math.round(m % 60)} د`;

export function toCsv(events: CountEvent[]): string {
  const rows = events.map((e) =>
    [e.id ?? "", e.camera_id, e.track_id, e.event_type, e.timestamp, e.meta?.frame ?? "", e.meta?.anchor?.[0] ?? "", e.meta?.anchor?.[1] ?? ""].join(","));
  return "\uFEFFid,camera_id,track_id,event_type,timestamp,frame,anchor_x,anchor_y\n" + rows.join("\n");
}

export const ago = (ms: number) => {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s} ث` : s < 3600 ? `${Math.floor(s / 60)} د` : `${Math.floor(s / 3600)} س`;
};
