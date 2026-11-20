/** Hall-count samples as returned by room-count/history: a time and how many people the hall camera saw. */
export interface Sample { t: string | number; count: number }
export interface HourStat { hour: number; n: number; avg: number; max: number }
export interface PeakResult {
  /** always 24 entries, index = hour of day (local time); n = 0 means no samples in that hour */
  hours: HourStat[];
  samples: number;
  days: number;
  peak: HourStat | null;
  quiet: HourStat | null;
  top: HourStat[];
}

/** Epoch milliseconds from an ISO string, epoch ms, or epoch seconds. NaN when it cannot be read. */
export function toMs(t: string | number): number {
  if (typeof t === "number") return t < 1e11 ? t * 1000 : t;
  return Date.parse(t);
}

export const hourLabel = (h: number) => `${String(h).padStart(2, "0")}:00`;
export const fmtAvg = (n: number) => (n >= 10 ? String(Math.round(n)) : String(Math.round(n * 10) / 10));

/** Average people in the hall for each hour of the day, across every sample given. */
export function hourlyStats(samples: readonly Sample[] | undefined): PeakResult {
  const sum = new Array<number>(24).fill(0), n = new Array<number>(24).fill(0), max = new Array<number>(24).fill(0);
  const days = new Set<string>();
  let total = 0;
  for (const s of samples ?? []) {
    const ms = toMs(s.t), c = Number(s.count);
    if (!Number.isFinite(ms) || !Number.isFinite(c)) continue;
    const d = new Date(ms), h = d.getHours();
    sum[h] += c; n[h] += 1; if (c > max[h]) max[h] = c;
    days.add(d.toDateString());
    total += 1;
  }
  const hours: HourStat[] = sum.map((s, h) => ({ hour: h, n: n[h], avg: n[h] ? s / n[h] : 0, max: max[h] }));
  const withData = hours.filter((h) => h.n > 0);
  const ranked = [...withData].sort((a, b) => b.avg - a.avg || a.hour - b.hour);
  return {
    hours, samples: total, days: days.size,
    peak: ranked[0] ?? null,
    top: ranked.filter((h) => h.avg > 0).slice(0, 3),
    quiet: withData.length > 1 ? ranked[ranked.length - 1] : null,
  };
}
