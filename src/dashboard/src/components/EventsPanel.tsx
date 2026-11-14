import { useMemo, useState } from "react";
import { toCsv } from "../lib/analytics";
import type { CountEvent } from "../types";

const fmt = (iso: string) =>
  new Date(iso).toLocaleString("ar-EG", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });

interface Props { events: CountEvent[]; paused: boolean; onPause: (p: boolean) => void; error: boolean }

export function EventsPanel({ events, paused, onPause, error }: Props) {
  const [type, setType] = useState("");
  const [q, setQ] = useState("");
  const rows = useMemo(
    () => events.filter((e) => (!type || e.event_type === type) && (!q || String(e.track_id).includes(q.trim()))),
    [events, type, q],
  );

  const exportCsv = () => {
    const url = URL.createObjectURL(new Blob([toCsv(rows)], { type: "text/csv;charset=utf-8" }));
    const a = document.createElement("a");
    a.href = url; a.download = `events-${new Date().toISOString().slice(0, 10)}.csv`; a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <>
      <div className="toolbar">
        <select value={type} onChange={(e) => setType(e.target.value)} aria-label="نوع الحدث">
          <option value="">الكل</option><option value="enter">دخول</option><option value="exit">خروج</option>
        </select>
        <input placeholder="بحث برقم track_id" value={q} onChange={(e) => setQ(e.target.value)} inputMode="numeric" />
        <button className="ghost" onClick={() => onPause(!paused)}>{paused ? "استئناف التحديث" : "إيقاف التحديث"}</button>
        <button onClick={exportCsv} disabled={!rows.length}>تصدير CSV ({rows.length})</button>
      </div>
      {error ? <p className="alert" role="alert">تعذّر تحميل الأحداث. تأكد من الـ backend ومن CORS.</p>
        : !rows.length ? <p className="empty">لا توجد أحداث مطابقة.</p> : (
        <div className="scroll"><table>
          <thead><tr><th>الوقت</th><th>الكاميرا</th><th>track_id</th><th>الحدث</th><th>Frame</th></tr></thead>
          <tbody>
            {rows.slice(0, 100).map((e, i) => (
              <tr key={e.id ?? `${e.timestamp}-${e.track_id}-${i}`}>
                <td>{fmt(e.timestamp)}</td><td>{e.camera_id}</td><td>{e.track_id}</td>
                <td><span className={`tag ${e.event_type}`}>{e.event_type === "enter" ? "دخول" : "خروج"}</span></td>
                <td>{e.meta?.frame ?? "–"}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
      {rows.length > 100 && <p className="muted">يعرض أحدث 100 من {rows.length}. استخدم التصدير للكل.</p>}
    </>
  );
}
