import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CamerasPanel, CrossingMap, HourlyChart, OccupancyChart } from "./components/Charts";
import { EventsPanel } from "./components/EventsPanel";
import { Insights } from "./components/Insights";
import { IconAlert, IconCam, IconClock, IconDownload, IconPeak, IconUsers, Logo } from "./components/Icons";
import { capStatus, Kpis, NowCard } from "./components/Kpis";
import { TablesSection } from "./components/TablesPanel";
import { OccupancyCards } from "./components/OccupancyCards";
import { Attention } from "./components/Attention";
import { TableToasts } from "./components/Toasts";
import { RoomCountChart } from "./components/RoomCountChart";
import { ConnectionBadge, OfflineBanner } from "./components/ConnectionBadge";
import { RecentFeed } from "./components/RecentFeed";
import { VideoPanel, type CameraWallApi } from "./components/VideoPanel";
import { PeakHours } from "./components/PeakHours";
import { IconPulse, StatusPage } from "./components/StatusPage";
import { Heatmap } from "./components/Widgets";
import { SourceBar } from "./components/SourceBar";
import { camLabel } from "./cameras";
import { config } from "./config";
import { I18nProvider, useI18n, type StringKey } from "./i18n";
import { useEvents } from "./hooks/useEvents";
import { useLive } from "./hooks/useLive";
import { useRoomHistory } from "./hooks/useRoomHistory";
import { useTables } from "./hooks/useTables";
import { usePersisted } from "./hooks/usePersisted";
import { useShortcuts } from "./hooks/useShortcuts";
import { useMark, useMarkAt, useSystemHealth } from "./hooks/useSystemHealth";
import { analyze, toCsv } from "./lib/analytics";
import { buildReportHtml, printReport } from "./lib/report";
import type { EventsQuery, Preset } from "./types";

type Page = "overview" | "camera" | "analytics" | "events" | "status";
const PAGES: [Page, StringKey, () => JSX.Element][] = [["overview", "nav.overview", IconUsers], ["camera", "nav.camera", IconCam], ["analytics", "nav.analytics", IconPeak], ["events", "nav.events", IconClock], ["status", "nav.status", IconPulse]];

function beep(f: number) {
  try { const c = new AudioContext(), o = c.createOscillator(), g = c.createGain(); o.frequency.value = f; o.connect(g); g.connect(c.destination); g.gain.setValueAtTime(0.15, c.currentTime); g.gain.exponentialRampToValueAtTime(0.001, c.currentTime + 0.5); o.start(); o.stop(c.currentTime + 0.5); } catch { /* audio unavailable */ }
}

const PRESETS: [Preset, StringKey][] = [["today", "range.today"], ["24h", "range.24h"], ["7d", "range.7d"], ["all", "range.all"]];

function rangeFor(p: Preset): EventsQuery {
  const now = Date.now();
  if (p === "all") return {};
  if (p === "today") { const d = new Date(); d.setHours(0, 0, 0, 0); return { from: d.toISOString() }; }
  return { from: new Date(now - (p === "24h" ? 1 : 7) * 86400_000).toISOString() };
}

function AppInner() {
  const { t, lang, dir, locale, toggle: toggleLang } = useI18n();
  const qc = useQueryClient();
  const { occupancy, room, status, retry, reconnect, error } = useLive();
  const roomHistory = useRoomHistory();
  const [preset, setPreset] = useState<Preset>("all");
  const [camera, setCamera] = useState("");
  const [paused, setPaused] = useState(false);
  const [cap, setCap] = usePersisted("dinesight.capacity", 50);
  const tables = useTables();
  const [longMin, setLongMin] = usePersisted("dinesight.longStayMin", 60);
  const [page, setPage] = useState<Page>("overview");
  const [present, setPresent] = useState(false);
  const [sound, setSound] = usePersisted("dinesight.sound", false);
  const [theme, setTheme] = usePersisted<"dark" | "light">("dinesight.theme", "dark");
  const [focusId, setFocusId] = useState<string | null>(null); // camera open large on the Camera page (null = grid)
  const camApi = useRef<CameraWallApi | null>(null);
  const pendingFullscreen = useRef(false);
  const seen = useRef<Record<string, number>>({}); // when each kind of data last arrived, for the status page

  const filter = useMemo<EventsQuery>(() => ({ ...rangeFor(preset), cameraId: camera || undefined }), [preset, camera]);
  const events = useEvents(filter, paused);
  const list = events.data ?? [];
  // overview + source bar always look at the latest events, whatever range the Analytics page has selected
  const recent = useEvents({}, paused);
  const recentList = recent.data ?? [];
  const a = useMemo(() => analyze(list, occupancy?.current ?? 0), [list, occupancy?.current]);
  useMark(seen, "occupancy", occupancy);
  useMark(seen, "room", room);
  useMark(seen, "tables", tables.tables);
  useMarkAt(seen, "events", events.dataUpdatedAt);
  useMarkAt(seen, "history", roomHistory.dataUpdatedAt);
  const health = useSystemHealth({ ws: String(status), active: page === "status" });

  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);
  // a new live message means a new event: refresh the lists right away
  useEffect(() => { if (!paused && occupancy) qc.invalidateQueries({ queryKey: ["events"] }); },
    [occupancy?.entered, occupancy?.exited, paused, qc]); // eslint-disable-line react-hooks/exhaustive-deps

  const exportReport = () => {
    const url = URL.createObjectURL(new Blob([toCsv(list)], { type: "text/csv;charset=utf-8" }));
    const link = Object.assign(document.createElement("a"), { href: url, download: `dinesight-report-${new Date().toISOString().slice(0, 10)}.csv` });
    link.click();
    URL.revokeObjectURL(url);
  };
  const exportPdf = () => {
    const cam = config.cameras.find((c) => c.id === camera);
    const html = buildReportHtml({
      t, lang, dir, locale, cap, from: filter.from,
      rangeLabel: t(PRESETS.find((p) => p[0] === preset)![1]),
      cameraLabel: cam ? camLabel(cam, lang) : t("cam.all"),
      occupancy, inside: occInside?.current, samples: roomHistory.data ?? [], events: list,
    });
    printReport(html, `dinesight-shift-report-${new Date().toISOString().slice(0, 10)}`);
  };

  // keyboard: 1-8 open a camera, G = grid, F = fullscreen (works from any page)
  useShortcuts({
    camera: (i) => { const c = config.cameras[i]; if (!c) return; setFocusId(c.id); setPage("camera"); },
    grid: () => { setFocusId(null); setPage("camera"); },
    fullscreen: () => {
      if (page === "camera" && camApi.current) camApi.current.fullscreen();
      else { pendingFullscreen.current = true; setPage("camera"); }
    },
  });
  useEffect(() => {
    if (page === "camera" && pendingFullscreen.current) { pendingFullscreen.current = false; camApi.current?.fullscreen(); }
  }, [page]);
  // "inside" = what the hall camera really sees; falls back to the entrance count until the first room_count arrives
  const lastSample = roomHistory.data?.[roomHistory.data.length - 1];
  const hallPoint = room ?? lastSample;
  const inside = (room ?? lastSample)?.count ?? occupancy?.current;
  const occInside = useMemo(() => (occupancy ? { ...occupancy, current: inside ?? occupancy.current } : undefined), [occupancy, inside]);
  const capInfo = capStatus(inside ?? 0, cap);
  const lastLevel = useRef(capInfo.level);
  useEffect(() => {
    if (sound && lastLevel.current !== capInfo.level && capInfo.level !== "ok") beep(capInfo.level === "full" ? 440 : 660);
    lastLevel.current = capInfo.level;
  }, [capInfo.level, sound]);
  useEffect(() => { document.documentElement.dataset.present = present ? "on" : "off"; }, [present]);

  if (!occupancy && !error && !config.useMock) return <div className="splash"><Logo /><b>DineSight</b></div>;

  return (
    <div className="shell">
      <aside className="side">
        <div className="brand"><Logo /><div><h1>DineSight</h1></div></div>
        <nav>
          {PAGES.map(([k, l, Ico]) => <button key={k} className={`nav ${page === k ? "on" : ""}`} onClick={() => setPage(k)}><Ico />{t(l)}{k === "status" && health.level !== "ok" && <i className={`nav-dot ${health.level}`} aria-hidden="true" />}</button>)}
        </nav>
      </aside>
      <main>
        <header>
          <div className="head-l"><h1 className="page-title">{t(PAGES.find((p) => p[0] === page)![1])}</h1></div>
          <div className="head-r">
            <ConnectionBadge status={status} retry={retry} />
            <button className="ghost" onClick={() => setSound(!sound)} aria-pressed={sound}>{sound ? t("hdr.soundOn") : t("hdr.soundOff")}</button>
            <button className="ghost" onClick={() => setPresent(!present)}>{present ? t("hdr.presentExit") : t("hdr.present")}</button>
            <button className="ghost btn-ico" onClick={exportReport} disabled={!list.length}><IconDownload /> {t("hdr.csv")}</button>
            <button className="ghost btn-ico" onClick={exportPdf} disabled={!list.length && !occupancy && !(roomHistory.data?.length)}><IconDownload /> {t("hdr.pdf")}</button>
            <button className="ghost" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>{theme === "dark" ? t("hdr.light") : t("hdr.dark")}</button>
            <button className="ghost" onClick={toggleLang} lang={lang === "ar" ? "en" : "ar"} title={t("hdr.langTitle")}>{t("hdr.lang")}</button>
          </div>
        </header>

        {error && <p className="alert" role="alert">{t("err.occupancy", { url: config.apiUrl })}</p>}
        <OfflineBanner status={status} retry={retry} onRetry={reconnect} />
        <SourceBar status={status} events={recentList} />

        {(page === "analytics" || page === "events") && (
          <div className="range">
            <div className="chips" role="group" aria-label={t("range.label")}>
              {PRESETS.map(([k, l]) => <button key={k} className={`chip ${preset === k ? "on" : ""}`} onClick={() => setPreset(k)}>{t(l)}</button>)}
            </div>
            <select value={camera} onChange={(e) => setCamera(e.target.value)} aria-label={t("cam.label")}>
              <option value="">{t("cam.all")}</option>
              {config.cameras.map((c, i) => <option key={c.id} value={c.id}>{i + 1}. {camLabel(c, lang)}</option>)}
            </select>
          </div>
        )}

        {capInfo.level !== "ok" && (
          <p className={`cap-alert ${capInfo.level}`} role="alert"><IconAlert /> {capInfo.level === "full" ? t("cap.full") : t("cap.high")} ({t("cap.pct", { pct: capInfo.pct })})</p>
        )}

        <div className="page" key={page}>
          {page === "overview" && <>
            <OccupancyCards occ={occupancy} room={hallPoint} status={status} />
            <Attention tables={tables.tables} longMin={longMin} entrance={occupancy?.current} hall={hallPoint?.count} hallAt={hallPoint?.t} />
            <TablesSection data={tables} longMin={longMin} onLongMin={setLongMin} />
            <div className="grid2">
              <RoomCountChart query={roomHistory} />
              <RecentFeed events={recentList} />
            </div>
          </>}
          {page === "camera" && (
            <div className="hero"><VideoPanel occ={occInside} focusId={focusId} onFocus={setFocusId} apiRef={camApi} /><NowCard occ={occInside} cap={cap} setCap={setCap} /></div>
          )}
          {page === "analytics" && <>
            <Kpis occ={occupancy} a={a} />
            <PeakHours samples={roomHistory.data} loading={roomHistory.isLoading} error={roomHistory.isError} />
            <div className="grid2">
              <section className="card"><h2>{t("sec.occTime")}</h2><OccupancyChart series={a.series} cap={cap} /></section>
              <section className="card"><h2>{t("sec.traffic")}</h2><HourlyChart data={a.hourly} /></section>
            </div>
            <section className="card"><h2>{t("sec.busy")}</h2><Heatmap data={a.heat} /></section>
            <div className="card grid3">
              <section><h2>{t("sec.cameras")}</h2><CamerasPanel cameras={a.cameras} /></section>
              <section><h2>{t("sec.crossing")}</h2><CrossingMap points={a.crossings} /></section>
              <section><h2>{t("sec.quality")}</h2><Insights a={a} /></section>
            </div>
          </>}
          {page === "status" && <StatusPage health={health} seen={seen} ws={String(status)} />}
          {page === "events" && <section className="card"><h2>{t("sec.events")}</h2>
            <EventsPanel events={list} paused={paused} onPause={setPaused} error={events.isError} />
          </section>}
        </div>
      </main>
      <TableToasts tables={tables.tables} />
    </div>
  );
}

export default function App() {
  return <I18nProvider><AppInner /></I18nProvider>;
}
