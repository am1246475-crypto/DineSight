import { fmtMin, type Analytics } from "../lib/analytics";

export function Insights({ a }: { a: Analytics }) {
  const issues: string[] = [];
  if (a.min < 0) issues.push("More exits than entries: some entries were missed.");
  if (a.flicker.length) issues.push(`${a.flicker.length} people crossed the line 4+ times: tracking may be unstable.`);
  return (
    <div className="insights">
      <p>Avg stay <b>{fmtMin(a.dwell.avgMin)}</b> · Median <b>{fmtMin(a.dwell.medianMin)}</b></p>
      {issues.length === 0
        ? <p className="ok">No counting issues detected.</p>
        : <ul className="issues">{issues.map((t) => <li key={t}>{t}</li>)}</ul>}
      {a.flicker.length > 0 && <p className="muted">Most crossings: {a.flicker.slice(0, 5).map((f) => `#${f.track} (${f.count})`).join(", ")}</p>}
    </div>
  );
}
