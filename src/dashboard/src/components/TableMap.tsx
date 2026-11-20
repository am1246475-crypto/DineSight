import { config } from "../config";
import { bbox, centroid, fmtDuration, sinceMs, toPoints } from "../lib/tables";
import type { TableView } from "../types";

// The SVG viewBox has the SAME aspect ratio as the camera frame (16:9 for 768x432), and the wrapper keeps that ratio
// with CSS aspect-ratio. The 0..1 polygon coordinates are therefore scaled uniformly: nothing stretches or shifts
// when the window is resized.
const VB_W = 1000;
const VB_H = (VB_W * config.frame.h) / config.frame.w;
export const isLong = (t: TableView, now: number, limitMin: number) => {
  const d = sinceMs(t.since, now);
  return t.status === "occupied" && d !== null && d >= limitMin * 60_000;
};
export const peopleLabel = (n: number) => `${n} ${n === 1 ? "person" : "people"}`;

interface Props {
  tables: TableView[];
  now: number;
  longMin: number;
  filter: "all" | "free" | "occupied";
  selected: string | null;
  changed: string[];
  onSelect: (id: string | null) => void;
}

export function TableMap({ tables, now, longMin, filter, selected, changed, onSelect }: Props) {
  return (
    <div className="tmap" style={{ aspectRatio: `${config.frame.w} / ${config.frame.h}` }}>
      <svg viewBox={`0 0 ${VB_W} ${VB_H}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="Table map"
           onClick={(e) => { if (e.target === e.currentTarget) onSelect(null); }}>
        {tables.map((t) => {
          const [cx, cy] = centroid(t.polygon);
          const b = bbox(t.polygon);
          const x = cx * VB_W, y = cy * VB_H;
          const dur = sinceMs(t.since, now);
          const occupied = t.status === "occupied";
          // font size: limited by the table height (3 lines) and by its width (the longest line must fit)
          const widest = Math.max(t.name.length, occupied ? 0.8 * Math.max(peopleLabel(t.people).length, dur !== null ? fmtDuration(dur).length : 0) : 0, 4);
          const fs = Math.max(8, Math.min(26, (b.h * VB_H) / 4.2, (b.w * VB_W * 0.86) / (widest * 0.56)));
          const gap = fs * 0.95;
          const cls = [
            "tp", t.status, isLong(t, now, longMin) ? "long" : "", selected === t.id ? "sel" : "", changed.includes(t.id) ? "changed" : "",
            filter !== "all" && filter !== t.status ? "dim" : "",
          ].join(" ");
          const label = `${t.name}: ${occupied ? `occupied, ${peopleLabel(t.people)}${dur !== null ? `, ${fmtDuration(dur)}` : ""}` : t.status === "free" ? "free" : "status unknown"}`;
          return (
            <g key={t.id} className={cls} tabIndex={0} role="button" aria-label={label} aria-pressed={selected === t.id}
               onClick={() => onSelect(selected === t.id ? null : t.id)}
               onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(selected === t.id ? null : t.id); } }}>
              <title>{label}</title>
              <polygon points={toPoints(t.polygon, VB_W, VB_H)} vectorEffect="non-scaling-stroke" />
              <g className="tt" textAnchor="middle" dominantBaseline="central" pointerEvents="none">
                <text x={x} y={occupied ? y - gap : y - gap * 0.5} fontSize={fs} className="tn">{t.name}</text>
                {occupied ? <>
                  <text x={x} y={y} fontSize={fs * 0.8} className="ts">{peopleLabel(t.people)}</text>
                  {dur !== null && <text x={x} y={y + gap * 0.9} fontSize={fs * 0.8} className="ts">{fmtDuration(dur)}</text>}
                </> : <text x={x} y={y + gap * 0.5} fontSize={fs * 0.8} className="ts">{t.status === "free" ? "Free" : "Unknown"}</text>}
              </g>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
