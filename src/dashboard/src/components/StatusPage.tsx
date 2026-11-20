import { useEffect, useState } from "react";
import { camLabel } from "../cameras";
import { config } from "../config";
import type { Health, Level, SeenMap } from "../hooks/useSystemHealth";
import { useI18n, type StringKey } from "../i18n";

export const IconPulse = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M3 12h4l3-8 4 16 3-8h4" />
  </svg>
);

function Row({ level, name, detail, state }: { level: Level; name: string; detail?: string; state: string }) {
  return (
    <li className={`sys-row ${level}`}>
      <i className="sys-dot" aria-hidden="true" />
      <div><b>{name}</b>{detail && <small dir="ltr">{detail}</small>}</div>
      <span className="sys-state">{state}</span>
    </li>
  );
}

const DATA: { key: string; label: StringKey; warnAfterSec?: number }[] = [
  { key: "occupancy", label: "sys.d.occupancy", warnAfterSec: 600 },
  { key: "room", label: "sys.d.room", warnAfterSec: 600 },
  { key: "events", label: "sys.d.events", warnAfterSec: 300 },
  { key: "history", label: "sys.d.history" },
  { key: "tables", label: "sys.d.tables" },
];

/** What is working, what is not, and when each kind of data last arrived. */
export function StatusPage({ health, seen, ws }: { health: Health; seen: SeenMap; ws: string }) {
  const { t, lang } = useI18n();
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(id); }, []);

  const ago = (at: number) => {
    const s = Math.max(0, Math.round((now - at) / 1000));
    if (s < 5) return t("sys.ago.now");
    if (s < 60) return t("sys.ago.s", { n: s });
    if (s < 3600) return t("sys.ago.m", { n: Math.floor(s / 60) });
    return t("sys.ago.h", { n: Math.floor(s / 3600) });
  };

  const backendState = config.useMock ? t("sys.mock")
    : !health.backend.at ? t("sys.checking")
    : health.backend.level === "ok" ? `${t("sys.up")} · ${t("sys.latency", { ms: health.backend.ms ?? 0 })}` : t("sys.down");
  const wsState = config.useMock ? t("sys.mock") : ws === "live" ? t("sys.wsLive") : ws === "offline" ? t("sys.wsOffline") : t("sys.wsOther");
  const summary = health.level === "ok" ? "sys.ok" : health.level === "warn" ? "sys.warn" : "sys.bad";

  return (
    <div className="sys">
      <p className={`sys-sum ${health.level}`} role="status">
        <i className="sys-dot" aria-hidden="true" /><b>{t(summary)}</b>
        <button className="ghost" onClick={health.checkNow}>{t("sys.checkNow")}</button>
      </p>

      <section className="card">
        <h2>{t("sys.services")}</h2>
        <ul className="sys-list">
          <Row level={health.backend.level} name={t("sys.backend")} detail={config.useMock ? undefined : config.apiUrl} state={backendState} />
          <Row level={health.ws} name={t("sys.ws")} state={wsState} />
        </ul>
      </section>

      <section className="card">
        <h2>{t("sys.cameras")}</h2>
        <ul className="sys-list">
          {config.cameras.map((c, i) => {
            const p = health.cams[c.id];
            const level: Level = !c.stream ? "idle" : p?.level ?? "idle";
            const state = !c.stream ? t("sys.noStream") : !p ? t("sys.checking")
              : p.level === "ok" ? `${t("sys.reachable")} · ${t("sys.latency", { ms: p.ms ?? 0 })}` : t("sys.unreachable");
            return <Row key={c.id} level={level} name={`${i + 1}. ${camLabel(c, lang)}`} detail={c.stream} state={state} />;
          })}
        </ul>
      </section>

      <section className="card">
        <h2>{t("sys.data")}</h2>
        <ul className="sys-list">
          {DATA.map((d) => {
            const at = seen.current[d.key];
            const quiet = at !== undefined && d.warnAfterSec !== undefined && (now - at) / 1000 > d.warnAfterSec;
            const level: Level = at === undefined ? "idle" : quiet ? "warn" : "ok";
            const state = at === undefined ? t("sys.never") : quiet ? `${ago(at)} · ${t("sys.quiet")}` : ago(at);
            return <Row key={d.key} level={level} name={t(d.label)} state={state} />;
          })}
        </ul>
      </section>
    </div>
  );
}
