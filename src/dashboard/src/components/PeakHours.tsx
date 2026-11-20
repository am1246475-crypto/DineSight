import { useMemo } from "react";
import { fmtAvg, hourLabel, hourlyStats, type Sample } from "../lib/peak";
import { useI18n } from "../i18n";
import { IconPeak } from "./Icons";

/** Average people in the hall for each hour of the day, from the room-count history. */
export function PeakHours({ samples, loading, error }: { samples?: readonly Sample[]; loading?: boolean; error?: boolean }) {
  const { t } = useI18n();
  const s = useMemo(() => hourlyStats(samples), [samples]);
  const topHours = new Set(s.top.map((h) => h.hour));
  const max = s.peak?.avg || 1;

  return (
    <section className="card">
      <h2><IconPeak /> {t("pk.title")}</h2>
      {error ? <p className="warn-box"><span>{t("pk.error")}</span></p>
        : loading && !s.peak ? <p className="muted">{t("pk.loading")}</p>
        : !s.peak || s.samples < 2 ? <p className="empty">{t("pk.empty")}</p>
        : <>
          <div className="rc-stats">
            <div><span>{t("pk.busiest")}</span><b dir="ltr">{hourLabel(s.peak.hour)}</b><small>{t("pk.people", { n: fmtAvg(s.peak.avg) })}</small></div>
            {s.quiet && <div><span>{t("pk.quietest")}</span><b dir="ltr">{hourLabel(s.quiet.hour)}</b><small>{t("pk.people", { n: fmtAvg(s.quiet.avg) })}</small></div>}
            <div><span>{t("pk.avgPeak")}</span><b>{fmtAvg(s.peak.avg)}</b><small>{t("pk.max")} {s.peak.max}</small></div>
          </div>

          <div className="pk" role="img" aria-label={`${t("pk.top")}: ${s.top.map((h) => `${hourLabel(h.hour)} (${t("pk.avg")} ${fmtAvg(h.avg)})`).join(", ")}`}>
            {s.hours.map((h) => (
              <div key={h.hour} className={`pk-col ${h.n ? "" : "none"} ${h.hour === s.peak!.hour ? "peak" : topHours.has(h.hour) ? "top" : ""}`}
                title={h.n ? `${hourLabel(h.hour)} · ${t("pk.avg")} ${fmtAvg(h.avg)} · ${t("pk.max")} ${h.max}` : hourLabel(h.hour)}>
                <i className="pk-bar" style={{ height: `${h.n ? Math.max(3, (h.avg / max) * 100) : 0}%` }} />
              </div>
            ))}
          </div>
          <div className="pk-lbl" aria-hidden="true">{s.hours.map((h) => <small key={h.hour}>{h.hour % 3 === 0 ? String(h.hour).padStart(2, "0") : ""}</small>)}</div>

          <p className="muted pk-note">{t("pk.basis", { n: s.samples, d: s.days })}</p>
        </>}
    </section>
  );
}
