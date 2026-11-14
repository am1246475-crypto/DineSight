import { useEffect, useState } from "react";
import { config } from "../config";
import { ago } from "../lib/analytics";
import type { ConnectionStatus, CountEvent } from "../types";

const WS: Record<ConnectionStatus, string> = { connecting: "جارٍ الاتصال", live: "متصل", offline: "مقطوع" };

/** Says where the numbers come from, and whether the pipeline looks alive. */
export function SourceBar({ status, events }: { status: ConnectionStatus; events: CountEvent[] }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(id); }, []);

  if (config.useMock) {
    return <div className="banner sim" role="status"><b>وضع المحاكاة:</b> الأرقام هنا مولّدة عشوائياً من المتصفح وليست من الكاميرا. غيّر VITE_USE_MOCK إلى false لقراءة البيانات الحقيقية.</div>;
  }
  const times = events.map((e) => Date.parse(e.timestamp));
  const last = times.length ? Math.max(...times) : null;
  const age = last === null ? null : now - last;
  const rate = times.filter((t) => now - t < 5 * 60_000).length / 5;
  return (
    <div className="banner real" role="status">
      <span>المصدر: <b>{config.apiUrl}</b></span>
      <span>WebSocket: <b className={status}>{WS[status]}</b></span>
      <span>آخر حدث: <b>{age === null ? "لا يوجد" : `منذ ${ago(age)}`}</b></span>
      <span>المعدل: <b>{rate.toFixed(1)}</b> حدث/د</span>
      {age !== null && age > 10 * 60_000 && <span className="stale">لا أحداث منذ فترة، تأكد أن الـ CV شغال ويرسل للـ API.</span>}
    </div>
  );
}
