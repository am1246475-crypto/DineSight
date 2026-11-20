import { useCallback, useEffect, useState, type MutableRefObject } from "react";
import { config } from "../config";

export type Level = "idle" | "ok" | "warn" | "bad";
export interface Probe { level: Level; ms?: number; at?: number }
export interface Health {
  backend: Probe;
  /** keyed by camera id; only cameras that have a stream address are probed */
  cams: Record<string, Probe>;
  ws: Level;
  /** worst of everything checked */
  level: Level;
  checkNow: () => void;
}

const RANK: Record<Level, number> = { idle: 0, ok: 1, warn: 2, bad: 3 };

/** Resolves with the response time in ms, rejects on network error, timeout, or (cors mode) a non-2xx answer. */
async function probe(url: string, cors: boolean, timeoutMs = 4000): Promise<number> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  const t0 = performance.now();
  try {
    // an MJPEG stream never ends, so a no-cors request is enough: it settles as soon as the server answers
    const res = await fetch(url, { cache: "no-store", signal: ctl.signal, ...(cors ? {} : { mode: "no-cors" as RequestMode }) });
    if (cors && !res.ok) throw new Error(`HTTP ${res.status}`);
    return Math.round(performance.now() - t0);
  } finally { clearTimeout(timer); ctl.abort(); }
}

/** `ws` is the status string from useLive ("live" = connected, "offline" = down, anything else = connecting). */
export function useSystemHealth({ ws, active }: { ws: string; active: boolean }): Health {
  const [backend, setBackend] = useState<Probe>({ level: "idle" });
  const [cams, setCams] = useState<Record<string, Probe>>({});
  const [tick, setTick] = useState(0);
  const checkNow = useCallback(() => setTick((n) => n + 1), []);

  // backend /health: always checked (cheap), so the menu can show a warning dot from any page
  useEffect(() => {
    if (config.useMock) return;
    let alive = true;
    const run = async () => {
      try { const ms = await probe(`${config.apiUrl}/health`, true); if (alive) setBackend({ level: "ok", ms, at: Date.now() }); }
      catch { if (alive) setBackend({ level: "bad", at: Date.now() }); }
    };
    run();
    const id = setInterval(run, 10_000);
    return () => { alive = false; clearInterval(id); };
  }, [tick]);

  // camera streams: only while the status page is open, so we do not open extra stream connections all day
  useEffect(() => {
    if (!active) return;
    let alive = true;
    const run = () => config.cameras.forEach(async (c) => {
      if (!c.stream) return;
      try { const ms = await probe(c.stream, false); if (alive) setCams((p) => ({ ...p, [c.id]: { level: "ok", ms, at: Date.now() } })); }
      catch { if (alive) setCams((p) => ({ ...p, [c.id]: { level: "bad", at: Date.now() } })); }
    });
    run();
    const id = setInterval(run, 15_000);
    return () => { alive = false; clearInterval(id); };
  }, [active, tick]);

  const wsLevel: Level = config.useMock ? "idle" : ws === "live" ? "ok" : ws === "offline" ? "bad" : "warn";
  const all: Level[] = [backend.level, wsLevel, ...Object.values(cams).map((c) => c.level)];
  const level = all.reduce<Level>((worst, l) => (RANK[l] > RANK[worst] ? l : worst), "idle");
  return { backend, cams, ws: wsLevel, level: level === "idle" ? "ok" : level, checkNow };
}

/** When each kind of data last arrived (ms since epoch). A mutable ref, read by the status page every second. */
export type SeenMap = MutableRefObject<Record<string, number>>;

/** Records "now" whenever `value` becomes a new (non-empty) value. Use for live data that has no timestamp of its own. */
export function useMark(seen: SeenMap, key: string, value: unknown) {
  useEffect(() => { if (value !== undefined && value !== null) seen.current[key] = Date.now(); }, [seen, key, value]);
}
/** Same, for react-query results: pass their `dataUpdatedAt` (0 = nothing fetched yet). */
export function useMarkAt(seen: SeenMap, key: string, updatedAt: number | undefined) {
  useEffect(() => { if (updatedAt && updatedAt > 0) seen.current[key] = updatedAt; }, [seen, key, updatedAt]);
}
