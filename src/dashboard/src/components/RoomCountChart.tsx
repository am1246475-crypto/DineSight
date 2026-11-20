import { useMemo, useState } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis, type TooltipProps } from "recharts";
import { useNow } from "../hooks/useNow";
import type { useRoomHistory } from "../hooks/useRoomHistory";
import type { RoomCountPoint } from "../types";
import { IconPeak } from "./Icons";
import { ErrorBox, SkeletonBox } from "./States";

const RANGES = [["1h", "1 hour", 3600_000], ["6h", "6 hours", 6 * 3600_000], ["24h", "24 hours", 24 * 3600_000], ["all", "All", Infinity]] as const;
type R = (typeof RANGES)[number][0];

const hm = (t: number) => new Date(t).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
const hms = (t: number) => new Date(t).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const day = (t: number) => new Date(t).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });

function Tip({ active, payload }: TooltipProps<number, string>) {
  const p = payload?.[0]?.payload as RoomCountPoint | undefined;
  if (!active || !p) return null;
  return (
    <div className="chart-tip">
      <b>{p.count} {p.count === 1 ? "person" : "people"}</b>
      <span>{hms(p.t)} · {day(p.t)}</span>
    </div>
  );
}

/** People inside the hall (hall camera) over time. Data: GET /room-count/history, extended live by WebSocket "room_count". */
export function RoomCountChart({ query }: { query: ReturnType<typeof useRoomHistory> }) {
  const [range, setRange] = useState<R>("6h");
  const now = useNow(30_000);
  const span = RANGES.find((r) => r[0] === range)![2];
  const all = query.data;
  const pts = useMemo(() => (all ?? []).filter((p) => p.t >= now - span), [all, now, span]);
  const stat = useMemo(() => {
    if (!pts.length) return null;
    const v = pts.map((p) => p.count);
    return { last: pts[pts.length - 1], peak: Math.max(...v), low: Math.min(...v), avg: v.reduce((s, x) => s + x, 0) / v.length };
  }, [pts]);

  return (
    <section className="card" aria-label="Room count">
      <div className="sec-head">
        <h2><IconPeak /> People inside over time</h2>
        <div className="chips" role="group" aria-label="Time range">
          {RANGES.map(([k, l]) => <button key={k} className={`chip ${range === k ? "on" : ""}`} aria-pressed={range === k} onClick={() => setRange(k)}>{l}</button>)}
        </div>
      </div>
      {query.isError && !all ? <ErrorBox title="Couldn't load the room count history" error={query.error} onRetry={() => query.refetch()} busy={query.isFetching} />
        : query.isLoading ? <SkeletonBox height={260} label="Loading chart" />
        : !pts.length ? <p className="empty"><span className="empty-ico"><IconPeak /></span>{all?.length ? "No samples in this time range. Try a longer one." : "No room count data yet."}</p>
        : <>
          {query.isError && <p className="warn-box" role="alert"><span>Could not refresh the history; showing the last data received.</span><button className="ghost" onClick={() => query.refetch()}>Try again</button></p>}
          {stat && (
            <div className="rc-stats">
              <div><span>Latest</span><b>{stat.last.count}</b><small>at {hms(stat.last.t)}</small></div>
              <div><span>Peak</span><b>{stat.peak}</b></div>
              <div><span>Lowest</span><b>{stat.low}</b></div>
              <div><span>Average</span><b>{stat.avg.toFixed(1)}</b></div>
            </div>
          )}
          <div className="chart"><ResponsiveContainer width="100%" height={260}>
            <AreaChart data={pts} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
              <defs>
                <linearGradient id="rcFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.45} />
                  <stop offset="100%" stopColor="var(--accent)" stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="var(--line)" vertical={false} />
              <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={hm} minTickGap={48}
                     stroke="var(--muted)" fontSize={11} />
              <YAxis stroke="var(--muted)" fontSize={11} allowDecimals={false} width={44} domain={[0, (m: number) => Math.max(m, 4)]}
                     label={{ value: "People", angle: -90, position: "insideLeft", fill: "var(--muted)", fontSize: 11 }} />
              <Tooltip content={<Tip />} />
              <Area type="stepAfter" dataKey="count" stroke="var(--accent)" fill="url(#rcFill)" strokeWidth={2.5} isAnimationActive={false}
                    activeDot={{ r: 5 }} />
            </AreaChart>
          </ResponsiveContainer></div>
        </>}
    </section>
  );
}
