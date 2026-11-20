import { config } from "../config";
import { useNow } from "../hooks/useNow";
import type { Retry } from "../hooks/useLive";
import type { ConnectionStatus } from "../types";

const secs = (retry: Retry | undefined, now: number) => (retry ? Math.max(0, Math.ceil((retry.at - now) / 1000)) : 0);

/** Small status indicator for the header: Live / Connecting / Reconnecting in Ns. */
export function ConnectionBadge({ status, retry }: { status: ConnectionStatus; retry?: Retry }) {
  const now = useNow(1000);
  const text = config.useMock ? "Demo data" : status === "live" ? "Live" : status === "connecting" ? "Connecting…"
    : retry ? `Offline · retrying in ${secs(retry, now)}s` : "Offline";
  return <span className="status" role="status" aria-live="polite"><span className={`dot ${config.useMock ? "live" : status}`} />{text}</span>;
}

/** Shown under the header while the live feed is down, so nobody trusts stale numbers by mistake. */
export function OfflineBanner({ status, retry, onRetry }: { status: ConnectionStatus; retry?: Retry; onRetry: () => void }) {
  const now = useNow(1000);
  if (config.useMock || status !== "offline") return null;
  return (
    <div className="offline-banner" role="alert">
      <span><b>Live connection lost.</b> The numbers below may be out of date. {retry ? `Reconnecting in ${secs(retry, now)}s (attempt ${retry.attempt}).` : "Reconnecting…"}</span>
      <button className="ghost" onClick={onRetry}>Retry now</button>
    </div>
  );
}
