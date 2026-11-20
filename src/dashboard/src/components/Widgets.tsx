import { useEffect, useRef, useState } from "react";

/** Animates a number from its previous value to the new one. */
export function useCountUp(v?: number) {
  const [d, setD] = useState(v ?? 0);
  const from = useRef(v ?? 0);
  useEffect(() => {
    if (v === undefined) return;
    const s = from.current, t0 = performance.now();
    let raf = 0;
    const step = (t: number) => {
      const k = Math.min(1, (t - t0) / 500), val = Math.round(s + (v - s) * (1 - Math.pow(1 - k, 3)));
      setD(val); from.current = val;
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [v]);
  return d;
}

export function Spark({ values }: { values: number[] }) {
  const max = Math.max(...values, 1), min = Math.min(...values, 0), r = max - min || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * 100},${26 - ((v - min) / r) * 24}`).join(" ");
  return <svg className="spark" viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true"><polyline points={pts} fill="none" stroke="currentColor" strokeWidth="2" vectorEffect="non-scaling-stroke" /></svg>;
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
export function Heatmap({ data }: { data: number[][] }) {
  const max = Math.max(...data.flat(), 1);
  if (max <= 1 && !data.flat().some(Boolean)) return <p className="empty">No data.</p>;
  return (
    <div className="heat">
      <span />{Array.from({ length: 24 }, (_, h) => <small key={h}>{h % 3 === 0 ? h : ""}</small>)}
      {data.map((row, d) => [<small key={`l${d}`}>{DAYS[d]}</small>, ...row.map((v, h) =>
        <i key={`${d}-${h}`} title={`${DAYS[d]} ${h}:00 · ${v}`} style={{ opacity: v ? 0.15 + (v / max) * 0.85 : 0.06 }} />)])}
    </div>
  );
}
