import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CamerasPanel, CrossingMap, HourlyChart, OccupancyChart } from "./components/Charts";
import { EventsPanel } from "./components/EventsPanel";
import { Insights } from "./components/Insights";
import { Kpis } from "./components/Kpis";
import { SourceBar } from "./components/SourceBar";
import { config } from "./config";
import { useEvents } from "./hooks/useEvents";
import { useLiveOccupancy } from "./hooks/useLiveOccupancy";
import { usePersisted } from "./hooks/usePersisted";
import { analyze } from "./lib/analytics";
import type { ConnectionStatus, EventsQuery, Preset } from "./types";

const STATUS: Record<ConnectionStatus, string> = { connecting: "جارٍ الاتصال", live: "متصل لحظياً", offline: "الاتصال مقطوع، جارٍ إعادة المحاولة" };
const PRESETS: [Preset, string][] = [["today", "اليوم"], ["24h", "آخر 24 ساعة"], ["7d", "7 أيام"], ["all", "الكل"]];

function rangeFor(p: Preset): EventsQuery {
  const now = Date.now();
  if (p === "all") return {};
  if (p === "today") { const d = new Date(); d.setHours(0, 0, 0, 0); return { from: d.toISOString() }; }
  return { from: new Date(now - (p === "24h" ? 1 : 7) * 86400_000).toISOString() };
}

export default function App() {
  const qc = useQueryClient();
  const { occupancy, status, error } = useLiveOccupancy();
  const [preset, setPreset] = useState<Preset>("today");
  const [camera, setCamera] = useState("");
  const [paused, setPaused] = useState(false);
  const [theme, setTheme] = usePersisted<"dark" | "light">("dinesight.theme", "dark");

  const filter = useMemo<EventsQuery>(() => ({ ...rangeFor(preset), cameraId: camera || undefined }), [preset, camera]);
  const events = useEvents(filter, paused);
  const list = events.data ?? [];
  const a = useMemo(() => analyze(list, occupancy?.current ?? 0), [list, occupancy?.current]);

  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);
  // a new live message means a new event: refresh the lists right away
  useEffect(() => { if (!paused && occupancy) qc.invalidateQueries({ queryKey: ["events"] }); },
    [occupancy?.entered, occupancy?.exited, paused, qc]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <main>
      <header>
        <div className="brand"><span className="logo">D</span><div><h1>DineSight</h1><small>إشغال المطعم لحظة بلحظة</small></div></div>
        <div className="head-r">
          <span className="status"><span className={`dot ${status}`} />{config.useMock ? "بيانات تجريبية" : STATUS[status]}</span>
          <button className="ghost" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>{theme === "dark" ? "فاتح" : "غامق"}</button>
        </div>
      </header>

      {error && <p className="alert" role="alert">تعذّر تحميل الإشغال. تأكد أن الـ backend شغال على {config.apiUrl}.</p>}

      <SourceBar status={status} events={list} />

      <div className="range">
        <div className="chips" role="group" aria-label="الفترة">
          {PRESETS.map(([k, l]) => <button key={k} className={`chip ${preset === k ? "on" : ""}`} onClick={() => setPreset(k)}>{l}</button>)}
        </div>
        <select value={camera} onChange={(e) => setCamera(e.target.value)} aria-label="الكاميرا">
          <option value="">كل الكاميرات</option>
          {config.cameras.map((c) => <option key={c}>{c}</option>)}
        </select>
      </div>

      <Kpis occ={occupancy} a={a} />

      <div className="grid2">
        <section className="card"><h2>الإشغال خلال الفترة</h2><OccupancyChart series={a.series} /></section>
        <section className="card"><h2>الحركة حسب ساعة اليوم</h2><HourlyChart data={a.hourly} /></section>
      </div>
      <div className="grid3">
        <section className="card"><h2>الكاميرات</h2><CamerasPanel cameras={a.cameras} /></section>
        <section className="card"><h2>أين يعبر الناس الخط؟</h2><CrossingMap points={a.crossings} /></section>
        <section className="card"><h2>جودة العدّ</h2><Insights a={a} /></section>
      </div>

      <section className="card"><h2>سجل الأحداث</h2>
        <EventsPanel events={list} paused={paused} onPause={setPaused} error={events.isError} />
      </section>
    </main>
  );
}
