import { useEffect, useRef, useState, type ReactNode } from "react";
import { fmtMin, type Analytics } from "../lib/analytics";
import type { Occupancy } from "../types";
import { Spark, useCountUp } from "./Widgets";
import { IconClock, IconIn, IconOut, IconPeak, IconRefresh, IconUsers } from "./Icons";

const when = (iso?: string | null) =>
  iso ? new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–";

export function capStatus(cur: number, cap: number) {
  const pct = cap > 0 ? Math.min(100, Math.round((cur / cap) * 100)) : 0;
  const level = pct >= 100 ? "full" : pct >= 80 ? "high" : "ok";
  const msg = { full: "At full capacity", high: "Near capacity", ok: "Comfortable" }[level];
  return { pct, level, msg } as const;
}

/** true for a moment after `v` changes (used to flash a number when it updates). */
export function useFlash(v: unknown) {
  const [on, setOn] = useState(false);
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    setOn(true);
    const t = setTimeout(() => setOn(false), 700);
    return () => clearTimeout(t);
  }, [v]);
  return on;
}

export const Skel = ({ big }: { big?: boolean }) => <span className={`skel ${big ? "big" : ""}`} />;

export function NowCard({ occ, cap, setCap }: { occ?: Occupancy; cap: number; setCap: (n: number) => void }) {
  const cur = occ?.current ?? 0;
  const { pct, level, msg } = capStatus(cur, cap);
  const flash = useFlash(occ?.current);
  const shown = useCountUp(occ?.current);
  return (
    <section className={`card now ${level}`}>
      <span className="k-label"><IconUsers /> Inside now</span>
      <div className="ring" style={{ ["--p" as string]: pct }} role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
        <div className="ring-in">
          {occ ? <div className={`num ${flash ? "flash" : ""}`}>{shown}</div> : <Skel big />}
          <small>{pct}%</small>
        </div>
      </div>
      <div className={`now-msg ${level}`}>{msg}</div>
      <div className="cap">
        <span><b>{Math.max(cap - cur, 0)}</b> spots left</span>
        <label>Capacity <input type="number" min={1} value={cap} onChange={(e) => setCap(Math.max(1, Number(e.target.value) || 1))} /></label>
      </div>
    </section>
  );
}

function Kpi({ icon, label, value, tone, spark, delta }: { icon: ReactNode; label: string; value?: string | number; tone?: string; spark?: number[]; delta?: number }) {
  const flash = useFlash(value);
  const isNum = typeof value === "number";
  const c = useCountUp(isNum ? value : undefined);
  return (
    <div className={`card kpi ${tone ? `t-${tone}` : ""}`}>
      <span className="k-label">{icon}{label}</span>
      {value === undefined ? <Skel /> : (
        <div className="k-row">
          <b className={`${tone ?? ""} ${flash ? "flash" : ""}`}>{isNum ? c : value}</b>
          {!!delta && <span className={`delta ${delta > 0 ? "up" : "down"}`}>{delta > 0 ? "▲" : "▼"} {Math.abs(delta)}</span>}
        </div>
      )}
      {spark && <Spark values={spark} />}
    </div>
  );
}

export function Kpis({ occ, a }: { occ?: Occupancy; a: Analytics }) {
  return (
    <section className="kpis">
      <Kpi icon={<IconIn />} label="Total entered" value={occ?.entered} tone="enter" spark={a.trend.enter} delta={a.delta.enter} />
      <Kpi icon={<IconOut />} label="Total exited" value={occ?.exited} tone="exit" spark={a.trend.exit} delta={a.delta.exit} />
      <Kpi icon={<IconPeak />} label="Peak occupancy" value={a.series.length ? a.peak : "–"} spark={a.trend.occ} />
      <Kpi icon={<IconClock />} label="Avg. stay" value={fmtMin(a.dwell.avgMin)} />
      <Kpi icon={<IconRefresh />} label="Last update" value={when(occ?.updated_at)} tone="small" />
    </section>
  );
}
