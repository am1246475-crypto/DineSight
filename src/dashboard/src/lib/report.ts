import type { Lang, StringKey } from "../i18n/strings";
import { fmtAvg, hourLabel, hourlyStats, toMs, type Sample } from "./peak";

type T = (key: StringKey, vars?: Record<string, string | number>) => string;

export interface ReportInput {
  t: T; lang: Lang; dir: "ltr" | "rtl"; locale: string;
  rangeLabel: string; cameraLabel: string;
  /** ISO start of the selected range; samples older than this are left out (events are already filtered) */
  from?: string;
  occupancy?: { entered: number; exited: number };
  inside?: number; cap: number;
  samples: readonly Sample[];
  events: readonly unknown[];
}

// ---------- helpers ----------
const esc = (s: unknown) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string));
const pad = (n: number) => String(n).padStart(2, "0");
const hhmm = (ms: number) => { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const ddmm = (ms: number) => { const d = new Date(ms); return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}`; };
const C = { enter: "#0E8A6B", exit: "#B5573A", accent: "#226C86", cap: "#B93A2A", grid: "#D3DDDB", ink: "#12201F", muted: "#566865" };

// Events come from the backend; their exact field names live in src/types.ts. These readers accept the usual
// spellings so the report works without importing that type. If a field is missing the traffic chart is skipped.
const pick = (o: unknown, keys: string[]): unknown => {
  if (!o || typeof o !== "object") return undefined;
  for (const k of keys) { const v = (o as Record<string, unknown>)[k]; if (v !== undefined && v !== null) return v; }
  return undefined;
};
function eventTime(e: unknown): number | null {
  const v = pick(e, ["ts", "timestamp", "t", "time", "created_at", "occurred_at", "at"]);
  const ms = typeof v === "number" || typeof v === "string" ? toMs(v) : NaN;
  return Number.isFinite(ms) ? ms : null;
}
function eventDir(e: unknown): "enter" | "exit" | null {
  const v = pick(e, ["direction", "type", "event", "kind", "event_type"]);
  if (typeof v !== "string") return null;
  const s = v.toLowerCase();
  if (/^(enter|entry|entered|in)$/.test(s)) return "enter";
  if (/^(exit|exited|out|leave|left)$/.test(s)) return "exit";
  return null;
}

// ---------- SVG charts (inline, so they print crisply) ----------
const W = 720, M = { l: 38, r: 10, t: 12, b: 24 };
const niceMax = (v: number) => Math.max(1, Math.ceil(v / 4) * 4 || 4);

function yAxis(H: number, max: number) {
  let out = "";
  for (let i = 0; i <= 4; i++) {
    const y = M.t + ((H - M.t - M.b) * (4 - i)) / 4;
    out += `<line x1="${M.l}" x2="${W - M.r}" y1="${y}" y2="${y}" stroke="${C.grid}" stroke-width="1"/>` +
      `<text x="${M.l - 6}" y="${y + 3.5}" text-anchor="end" font-size="10" fill="${C.muted}">${fmtAvg((max * i) / 4)}</text>`;
  }
  return out;
}

function lineChart(points: { x: number; y: number }[], cap: number): string {
  if (points.length < 2) return "";
  const H = 210, pts = points.length > 240 ? points.filter((_, i) => i % Math.ceil(points.length / 240) === 0 || i === points.length - 1) : points;
  const x0 = pts[0].x, x1 = pts[pts.length - 1].x, max = niceMax(Math.max(cap, ...pts.map((p) => p.y)));
  const px = (x: number) => M.l + ((x - x0) / Math.max(1, x1 - x0)) * (W - M.l - M.r);
  const py = (y: number) => M.t + (1 - y / max) * (H - M.t - M.b);
  const line = pts.map((p) => `${px(p.x).toFixed(1)},${py(p.y).toFixed(1)}`).join(" ");
  const area = `${px(x0).toFixed(1)},${py(0)} ${line} ${px(x1).toFixed(1)},${py(0)}`;
  const multiDay = x1 - x0 > 36 * 3600_000;
  let ticks = "";
  for (let i = 0; i <= 4; i++) {
    const x = x0 + ((x1 - x0) * i) / 4;
    ticks += `<text x="${px(x).toFixed(1)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}" font-size="10" fill="${C.muted}">${multiDay ? ddmm(x) + " " : ""}${hhmm(x)}</text>`;
  }
  const capLine = cap > 0 ? `<line x1="${M.l}" x2="${W - M.r}" y1="${py(cap)}" y2="${py(cap)}" stroke="${C.cap}" stroke-width="1.4" stroke-dasharray="6 4"/>` : "";
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img">${yAxis(H, max)}${capLine}` +
    `<polygon points="${area}" fill="${C.accent}" fill-opacity=".14"/><polyline points="${line}" fill="none" stroke="${C.accent}" stroke-width="2" stroke-linejoin="round"/>${ticks}</svg>`;
}

interface Series { values: number[]; color: string }
function barChart(series: Series[], highlight = -1): string {
  const H = 190, max = niceMax(Math.max(...series.flatMap((s) => s.values), 0));
  const slot = (W - M.l - M.r) / 24, bw = (slot * 0.76) / series.length;
  let bars = "", labels = "";
  for (let h = 0; h < 24; h++) {
    series.forEach((s, i) => {
      const v = s.values[h] || 0, hgt = (v / max) * (H - M.t - M.b);
      const x = M.l + h * slot + slot * 0.12 + i * bw;
      const color = highlight === h ? C.cap : s.color;
      if (v > 0) bars += `<rect x="${x.toFixed(1)}" y="${(H - M.b - hgt).toFixed(1)}" width="${(bw - 1).toFixed(1)}" height="${hgt.toFixed(1)}" rx="2" fill="${color}"/>`;
    });
    if (h % 3 === 0) labels += `<text x="${(M.l + h * slot + slot / 2).toFixed(1)}" y="${H - 6}" text-anchor="middle" font-size="10" fill="${C.muted}">${pad(h)}</text>`;
  }
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img">${yAxis(H, max)}${bars}${labels}</svg>`;
}

// ---------- the report ----------
export function buildReportHtml(r: ReportInput): string {
  const { t } = r;
  const fromMs = r.from ? Date.parse(r.from) : 0;
  const samples = r.samples.filter((s) => { const ms = toMs(s.t); return Number.isFinite(ms) && ms >= fromMs; });
  const pts = samples.map((s) => ({ x: toMs(s.t), y: Number(s.count) })).filter((p) => Number.isFinite(p.y)).sort((a, b) => a.x - b.x);
  const peakInside = pts.length ? Math.max(...pts.map((p) => p.y)) : undefined;
  const avgInside = pts.length ? pts.reduce((a, p) => a + p.y, 0) / pts.length : undefined;
  const hourly = hourlyStats(samples);

  // traffic by hour from the events (when their direction and time can be read)
  const enter = new Array<number>(24).fill(0), exit = new Array<number>(24).fill(0);
  let nIn = 0, nOut = 0;
  for (const e of r.events) {
    const ms = eventTime(e), dir = eventDir(e);
    if (ms === null || !dir) continue;
    const h = new Date(ms).getHours();
    if (dir === "enter") { enter[h]++; nIn++; } else { exit[h]++; nOut++; }
  }
  const haveTraffic = nIn + nOut > 0;
  const entered = haveTraffic ? nIn : r.occupancy?.entered;
  const exited = haveTraffic ? nOut : r.occupancy?.exited;
  const pctCap = r.inside !== undefined && r.cap > 0 ? Math.round((r.inside / r.cap) * 100) : undefined;

  const now = new Date();
  const stamp = `${now.toLocaleDateString(r.locale)} ${hhmm(now.getTime())}`;
  const dash = "—";
  const kpi = (label: string, value: unknown, sub = "") =>
    `<div class="kpi"><span>${esc(label)}</span><b>${esc(value ?? dash)}</b><small>${esc(sub)}</small></div>`;
  const noData = `<p class="none">${esc(t("rep.noData"))}</p>`;
  const legend = (items: [string, string][]) => `<div class="legend">${items.map(([c, l]) => `<span><i style="background:${c}"></i>${esc(l)}</span>`).join("")}</div>`;

  const body = `
<header>
  <div><h1>DineSight</h1><p class="sub">${esc(t("rep.title"))}</p></div>
  <dl>
    <div><dt>${esc(t("rep.range"))}</dt><dd>${esc(r.rangeLabel)}</dd></div>
    <div><dt>${esc(t("rep.camera"))}</dt><dd>${esc(r.cameraLabel)}</dd></div>
    <div><dt>${esc(t("rep.generated"))}</dt><dd dir="ltr">${esc(stamp)}</dd></div>
  </dl>
</header>
<section class="kpis">
  ${kpi(t("rep.kpiInside"), r.inside, pctCap !== undefined ? t("cap.pct", { pct: pctCap }) : "")}
  ${kpi(t("rep.kpiIn"), entered)}
  ${kpi(t("rep.kpiOut"), exited)}
  ${kpi(t("rep.kpiCap"), r.cap || undefined)}
  ${kpi(t("rep.kpiPeak"), peakInside)}
  ${kpi(t("rep.kpiAvg"), avgInside === undefined ? undefined : fmtAvg(avgInside))}
  ${kpi(t("rep.kpiPeakHour"), hourly.peak ? hourLabel(hourly.peak.hour) : undefined, hourly.peak ? `${t("pk.avg")} ${fmtAvg(hourly.peak.avg)}` : "")}
  ${kpi(t("rep.events"), r.events.length)}
</section>
<section>
  <h2>${esc(t("rep.chartRoom"))}</h2>
  ${pts.length > 1 ? `<div class="chart">${lineChart(pts, r.cap)}</div>${r.cap > 0 ? legend([[C.accent, t("rep.kpiInside")], [C.cap, t("rep.capLine")]]) : ""}` : noData}
</section>
<section>
  <h2>${esc(t("rep.chartTraffic"))}</h2>
  ${haveTraffic ? `<div class="chart">${barChart([{ values: enter, color: C.enter }, { values: exit, color: C.exit }])}</div>${legend([[C.enter, t("rep.entered")], [C.exit, t("rep.exited")]])}` : noData}
</section>
<section>
  <h2>${esc(t("rep.chartAvg"))}</h2>
  ${hourly.peak ? `<div class="chart">${barChart([{ values: hourly.hours.map((h) => h.avg), color: C.accent }], hourly.peak.hour)}</div>
  <p class="top"><b>${esc(t("rep.top"))}:</b> ${hourly.top.map((h) => `<span dir="ltr">${hourLabel(h.hour)}</span> (${esc(t("pk.avg"))} ${fmtAvg(h.avg)})`).join(" · ")}</p>` : noData}
</section>
<footer>${esc(t("rep.footer"))} · <span dir="ltr">${esc(stamp)}</span></footer>`;

  return `<!doctype html><html lang="${r.lang}" dir="${r.dir}"><head><meta charset="utf-8">
<title>dinesight-shift-report-${now.toISOString().slice(0, 10)}</title>
<style>
@page{size:A4;margin:14mm}
*{box-sizing:border-box}
body{margin:0;color:${C.ink};font:13px/1.5 "IBM Plex Sans","IBM Plex Sans Arabic",Tahoma,Arial,sans-serif;-webkit-print-color-adjust:exact;print-color-adjust:exact}
header{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;border-bottom:2px solid ${C.accent};padding-bottom:10px;margin-bottom:16px}
h1{margin:0;font-size:24px;color:${C.accent}}.sub{margin:0;color:${C.muted};font-size:15px}
dl{margin:0;display:flex;gap:18px}dl div{display:grid}dt{color:${C.muted};font-size:11px}dd{margin:0;font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:18px}
.kpi{border:1px solid ${C.grid};border-radius:10px;padding:10px 12px;display:grid;gap:2px;break-inside:avoid}
.kpi span{color:${C.muted};font-size:11px}.kpi b{font-size:22px;line-height:1.1}.kpi small{color:${C.muted};font-size:11px;min-height:14px}
section{margin-bottom:16px;break-inside:avoid}
h2{font-size:14px;margin:0 0 6px}
.chart{direction:ltr}.none{color:${C.muted};margin:4px 0}
.legend{display:flex;gap:16px;color:${C.muted};font-size:11px;margin-top:4px}.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-inline-end:6px;vertical-align:-1px}
.top{margin:6px 0 0}
footer{margin-top:18px;border-top:1px solid ${C.grid};padding-top:8px;color:${C.muted};font-size:11px}
</style></head><body>${body}</body></html>`;
}

/**
 * Opens the browser's print dialog for the report; choose "Save as PDF" there.
 * (Printing keeps Arabic shaping and right-to-left layout exactly as the browser draws them.)
 */
export function printReport(html: string, filename: string): void {
  const frame = document.createElement("iframe");
  frame.setAttribute("aria-hidden", "true");
  frame.style.cssText = "position:fixed;inset-inline-end:0;bottom:0;width:0;height:0;border:0;visibility:hidden";
  const previousTitle = document.title;
  const cleanup = () => { document.title = previousTitle; frame.remove(); };
  frame.onload = () => {
    const w = frame.contentWindow;
    if (!w) return cleanup();
    document.title = filename; // the browser proposes this as the PDF file name
    w.onafterprint = cleanup;
    w.focus();
    w.print();
    setTimeout(cleanup, 120_000);
  };
  frame.srcdoc = html;
  document.body.appendChild(frame);
}
