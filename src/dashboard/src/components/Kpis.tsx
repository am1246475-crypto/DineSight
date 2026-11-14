import { usePersisted } from "../hooks/usePersisted";
import { fmtMin, type Analytics } from "../lib/analytics";
import type { Occupancy } from "../types";

const when = (iso?: string | null) =>
  iso ? new Date(iso).toLocaleTimeString("ar-EG", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–";

export function Kpis({ occ, a }: { occ?: Occupancy; a: Analytics }) {
  const [cap, setCap] = usePersisted("dinesight.capacity", 50);
  const cur = occ?.current ?? 0;
  const pct = cap > 0 ? Math.min(100, Math.round((cur / cap) * 100)) : 0;
  const level = pct >= 100 ? "full" : pct >= 80 ? "high" : "ok";
  const msg = { full: "المطعم وصل للسعة القصوى", high: "اقتربت من السعة القصوى", ok: "السعة مريحة" }[level];

  return (
    <section className="kpis">
      <div className={`card now ${level}`}>
        <span className="k-label">موجودون الآن</span>
        <div className="num">{occ ? cur : "–"}</div>
        <div className="gauge" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}><i style={{ width: `${pct}%` }} /></div>
        <div className="cap">
          <span>{pct}% من السعة · {msg}</span>
          <label>السعة <input type="number" min={1} value={cap} onChange={(e) => setCap(Math.max(1, Number(e.target.value) || 1))} /></label>
        </div>
      </div>
      <div className="card"><span className="k-label">إجمالي الداخلين</span><b className="enter">{occ?.entered ?? "–"}</b></div>
      <div className="card"><span className="k-label">إجمالي الخارجين</span><b className="exit">{occ?.exited ?? "–"}</b></div>
      <div className="card"><span className="k-label">ذروة الإشغال (الفترة)</span><b>{a.series.length ? a.peak : "–"}</b></div>
      <div className="card"><span className="k-label">متوسط مدة البقاء</span><b>{fmtMin(a.dwell.avgMin)}</b><small>من {a.dwell.n} زائر</small></div>
      <div className="card"><span className="k-label">آخر تحديث</span><b className="small">{when(occ?.updated_at)}</b></div>
    </section>
  );
}
