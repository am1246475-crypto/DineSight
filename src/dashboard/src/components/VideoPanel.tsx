import { useCallback, useEffect, useRef, useState, type MutableRefObject } from "react";
import { config } from "../config";
import { camLabel, type CameraDef } from "../cameras";
import { useI18n, type StringKey } from "../i18n";
import type { Occupancy } from "../types";
import { IconCam } from "./Icons";

const Clock = () => {
  const { locale } = useI18n();
  const [now, setNow] = useState(new Date());
  useEffect(() => { const id = setInterval(() => setNow(new Date()), 1000); return () => clearInterval(id); }, []);
  return <span className="stamp">{now.toLocaleTimeString(locale)}</span>;
};

type Status = "live" | "offline" | "demo" | "none";
const STATUS_KEY: Record<Status, StringKey> = { live: "vid.live", offline: "vid.offline", demo: "vid.demo", none: "vid.none" };

interface ViewProps { cam: CameraDef; num: number; demo: boolean; focus?: boolean; line?: boolean; occ?: Occupancy; onOpen?: () => void }

/** One camera: its live MJPEG stream (retrying if it drops), a number badge, its name and a status. */
function CamView({ cam, num, demo, focus, line, occ, onOpen }: ViewProps) {
  const { t, lang } = useI18n();
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const hasStream = !!cam.stream;
  const name = camLabel(cam, lang);
  // the stream may start after the dashboard (or restart): try again every 5 s instead of giving up for good
  useEffect(() => {
    if (!failed) return;
    const id = setTimeout(() => { setFailed(false); setAttempt((a) => a + 1); }, 5000);
    return () => clearTimeout(id);
  }, [failed]);
  const src = cam.stream ? `${cam.stream}${cam.stream.includes("?") ? "&" : "?"}t=${attempt}` : "";
  const status: Status = hasStream ? (failed ? "offline" : "live") : demo ? "demo" : "none";
  const open = onOpen && {
    role: "button" as const, tabIndex: 0, "aria-label": t("vid.open", { n: num, name }), onClick: onOpen,
    onKeyDown: (e: React.KeyboardEvent) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); } },
  };
  return (
    <div className={`video cam ${focus ? "focus" : ""}`} {...open}>
      {hasStream ? (failed ? <div className="no-feed">{t("vid.retry")}</div>
          : <img src={src} alt={t("vid.alt", { n: num, name })} onError={() => setFailed(true)} />)
        : demo ? <video src={config.videoFile} autoPlay muted loop playsInline controls={focus} />
        : <div className="no-feed">{t("vid.noStream")}</div>}
      <span className="cam-num">{num}</span>
      <span className="cam-name">{name}</span>
      <span className={`cam-status ${status}`}>● {t(STATUS_KEY[status])}</span>
      {focus && line && <i className="count-line" style={{ left: `${config.lineX * 100}%` }} />}
      {focus && <Clock />}
      {focus && occ && <span className="vid-occ">{t("vid.inside")} <b>{occ.current}</b></span>}
    </div>
  );
}

export interface CameraWallApi { fullscreen: () => void }

interface PanelProps {
  occ?: Occupancy;
  /** which camera is open large (null = the grid); owned by App so the keyboard shortcuts can change it */
  focusId: string | null;
  onFocus: (id: string | null) => void;
  /** App calls apiRef.current.fullscreen() for the F shortcut */
  apiRef: MutableRefObject<CameraWallApi | null>;
}

/** All cameras as a numbered grid (works for 1 or 8+); click one to open it large, the others stay in a strip below. */
export function VideoPanel({ occ, focusId, onFocus, apiRef }: PanelProps) {
  const { t } = useI18n();
  const cams = config.cameras;
  const [line, setLine] = useState(true);
  const box = useRef<HTMLDivElement>(null);
  const wall = useRef<HTMLDivElement>(null);
  const anyStream = cams.some((c) => c.stream);
  // no real stream anywhere: show the sample recording so the page is not empty (every camera in mock mode, else camera 1)
  const demoFor = (i: number) => !anyStream && (config.useMock || i === 0);
  const effectiveId = cams.length === 1 ? cams[0].id : focusId;
  const focusIdx = cams.findIndex((c) => c.id === effectiveId);

  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); return; }
    (focusIdx >= 0 ? box.current : wall.current)?.requestFullscreen?.().catch(() => {});
  }, [focusIdx]);
  useEffect(() => { apiRef.current = { fullscreen: toggleFullscreen }; return () => { apiRef.current = null; }; }, [apiRef, toggleFullscreen]);

  return (
    <section className="card video-card" aria-label={t("vid.title")}>
      <div className="sec-head">
        <h2><IconCam /> {t("vid.title")} <em className="pill">{cams.length}</em></h2>
        <div className="sec-r">
          {focusIdx >= 0 ? <>
            {cams.length > 1 && <button className="chip" onClick={() => onFocus(null)}>{t("vid.all")}</button>}
            <button className={`chip ${line ? "on" : ""}`} aria-pressed={line} onClick={() => setLine(!line)}>{t("vid.line")}</button>
            <button className="chip" onClick={toggleFullscreen}>{t("vid.fs")}</button>
          </> : <span className="muted">{t("vid.pick")}</span>}
        </div>
      </div>
      <p className="kbd-hint" dir="auto">
        <kbd>1</kbd>–<kbd>8</kbd> {t("vid.kOpen")} <span aria-hidden="true">·</span> <kbd>G</kbd> {t("vid.kGrid")} <span aria-hidden="true">·</span> <kbd>F</kbd> {t("vid.kFs")}
      </p>

      {focusIdx < 0 ? (
        <div className="cam-wall" ref={wall}>
          <div className="cam-grid">
            {cams.map((c, i) => <CamView key={c.id} cam={c} num={i + 1} demo={demoFor(i)} onOpen={() => onFocus(c.id)} />)}
          </div>
        </div>
      ) : (
        <div className="cam-focus">
          <div ref={box}><CamView key={cams[focusIdx].id} cam={cams[focusIdx]} num={focusIdx + 1} demo={demoFor(focusIdx)} focus line={line} occ={occ} /></div>
          {cams.length > 1 && (
            <div className="cam-strip">
              {cams.map((c, i) => i === focusIdx ? null
                : <CamView key={c.id} cam={c} num={i + 1} demo={demoFor(i)} onOpen={() => onFocus(c.id)} />)}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
