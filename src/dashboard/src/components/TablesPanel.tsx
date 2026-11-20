import { useEffect, useMemo, useRef, useState } from "react";
import { config } from "../config";
import { useNow } from "../hooks/useNow";
import type { useTables } from "../hooks/useTables";
import { byName, fmtDuration, sinceMs, tablesToCsv, tableStats } from "../lib/tables";
import { IconDownload, IconTable } from "./Icons";
import { ErrorBox, SkeletonBox } from "./States";
import { isLong, peopleLabel, TableMap } from "./TableMap";

type F = "all" | "free" | "occupied";
const FILTERS: [F, string][] = [["all", "All"], ["free", "Free"], ["occupied", "Occupied"]];

/** ids whose status changed since the previous render; they stay flagged for 1.5 s (used to pulse the table). */
function useChanged(tables: { id: string; status: string }[]) {
  const [changed, setChanged] = useState<string[]>([]);
  const prev = useRef<Record<string, string>>({});
  useEffect(() => {
    const ch = tables.filter((t) => prev.current[t.id] && prev.current[t.id] !== t.status).map((t) => t.id);
    prev.current = Object.fromEntries(tables.map((t) => [t.id, t.status]));
    if (!ch.length) return;
    setChanged(ch);
    const id = setTimeout(() => setChanged([]), 1500);
    return () => clearTimeout(id);
  }, [tables]);
  return changed;
}

const LIMITS = [30, 45, 60, 90, 120];

export function TablesSection({ data, longMin, onLongMin }: { data: ReturnType<typeof useTables>; longMin: number; onLongMin: (m: number) => void }) {
  const { tables, isLoading, layoutError, statusError, layoutLoaded, refetch, isFetching } = data;
  const now = useNow(10_000);
  const [filter, setFilter] = useState<F>("all");
  const [selected, setSelected] = useState<string | null>(null);
  const changed = useChanged(tables);
  const sorted = useMemo(() => [...tables].sort(byName), [tables]);

  const busy = tables.filter((t) => t.status === "occupied");
  const free = tables.filter((t) => t.status === "free").length;
  const seated = busy.reduce((s, t) => s + t.people, 0);
  const { avg, longest } = tableStats(tables, now);
  const exportTables = () => {
    const url = URL.createObjectURL(new Blob([tablesToCsv(tables, Date.now())], { type: "text/csv;charset=utf-8" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: `dinesight-tables-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.csv` });
    a.click();
    URL.revokeObjectURL(url);
  };
  const pct = tables.length ? Math.round((busy.length / tables.length) * 100) : 0;
  const shown = sorted.filter((t) => filter === "all" || t.status === filter);

  const ratio = `${config.frame.w} / ${config.frame.h}`;
  return (
    <section className="card" aria-label="Tables">
      <div className="sec-head">
        <h2><IconTable /> Table map</h2>
        <div className="sec-r">
          {isFetching && !isLoading && <span className="pill">Refreshing…</span>}
          <label className="limit">Long stay after
            <select value={longMin} onChange={(e) => onLongMin(Number(e.target.value))} aria-label="Long stay limit in minutes">
              {LIMITS.map((m) => <option key={m} value={m}>{m} min</option>)}
            </select>
          </label>
          <button className="ghost btn-ico" onClick={exportTables} disabled={!tables.length}><IconDownload /> Export tables</button>
          <div className="chips" role="group" aria-label="Table filter">
            {FILTERS.map(([k, l]) => <button key={k} className={`chip ${filter === k ? "on" : ""}`} aria-pressed={filter === k} onClick={() => setFilter(k)}>{l}</button>)}
          </div>
        </div>
      </div>

      {layoutError ? <ErrorBox title="Couldn't load the table map" error={layoutError} onRetry={refetch} busy={isFetching} />
        : isLoading ? (
          <div className="tmap-grid"><SkeletonBox ratio={ratio} label="Loading table map" /><SkeletonBox height={260} label="Loading table list" /></div>
        ) : layoutLoaded && tables.length === 0 ? <p className="empty">No tables are configured yet.</p>
        : <>
          {statusError && (
            <div className="warn-box" role="alert">
              <span>Live table status is unavailable, so the tables are shown in grey. {statusError.message}</span>
              <button className="ghost" onClick={refetch} disabled={isFetching}>Try again</button>
            </div>
          )}
          <div className="tbl-stats">
            <div className="stat"><b>{tables.length}</b><span>Tables</span></div>
            <div className="stat free"><b>{free}</b><span>Free</span></div>
            <div className="stat occupied"><b>{busy.length}</b><span>Occupied</span></div>
            <div className="stat"><b>{seated}</b><span>People seated</span></div>
            {avg !== null && <div className="stat"><b>{fmtDuration(avg)}</b><span>Average stay</span></div>}
            {longest && <div className="stat"><b>{longest.name}</b><span>Longest, {fmtDuration(longest.ms)}</span></div>}
            <div className="gauge" aria-hidden="true"><i style={{ width: `${pct}%` }} /></div>
          </div>
          <div className="tmap-grid">
            <TableMap tables={tables} now={now} longMin={longMin} filter={filter} selected={selected} changed={changed} onSelect={setSelected} />
            <div className="tlist-wrap">
              <table className="tlist">
                <thead><tr><th>Table</th><th>Status</th><th>People</th><th>Occupied for</th></tr></thead>
                <tbody>
                  {shown.map((t) => {
                    const d = sinceMs(t.since, now);
                    return (
                      <tr key={t.id} className={`${selected === t.id ? "sel" : ""} ${changed.includes(t.id) ? "changed" : ""}`}
                          onClick={() => setSelected(selected === t.id ? null : t.id)}>
                        <td><b>{t.id}</b>{t.name !== t.id && <small>{t.name}</small>}</td>
                        <td><span className={`st ${t.status} ${isLong(t, now, longMin) ? "long" : ""}`}>{t.status === "occupied" ? "Occupied" : t.status === "free" ? "Free" : "Unknown"}</span></td>
                        <td>{t.status === "occupied" ? peopleLabel(t.people) : "–"}</td>
                        <td>{t.status === "occupied" && d !== null ? fmtDuration(d) : "–"}</td>
                      </tr>
                    );
                  })}
                  {shown.length === 0 && <tr><td colSpan={4} className="empty">No {filter} tables.</td></tr>}
                </tbody>
              </table>
            </div>
          </div>
          <div className="legend">
            <span><i className="sw free" />Free</span><span><i className="sw occupied" />Occupied</span><span><i className="sw long" />Over {longMin} min</span>
            {statusError && <span><i className="sw unknown" />Unknown</span>}
          </div>
        </>}
    </section>
  );
}
