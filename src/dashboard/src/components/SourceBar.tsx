import { useEffect, useState } from "react";
import { config } from "../config";
import { ago } from "../lib/analytics";
import type { ConnectionStatus, CountEvent } from "../types";

const WS: Record<ConnectionStatus, string> = { connecting: "Connecting", live: "Connected", offline: "Disconnected" };

/** Says where the numbers come from, and whether the pipeline looks alive. */
export function SourceBar({ status, events }: { status: ConnectionStatus; events: CountEvent[] }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(id); }, []);

  if (config.useMock) {
    return <div className="banner sim" role="status"><b>Demo mode:</b> numbers are simulated, not from the camera.</div>;
  }
  const times = events.map((e) => Date.parse(e.timestamp));
  const last = times.length ? Math.max(...times) : null;
  const age = last === null ? null : now - last;
  const rate = times.filter((t) => now - t < 5 * 60_000).length / 5;
  return (
    <div className="banner real" role="status">
      <span>Source: <b>{config.apiUrl}</b></span>
      <span>Live link: <b className={status}>{WS[status]}</b></span>
      <span>Last event: <b>{age === null ? "None" : `${ago(age)} ago`}</b></span>
      <span>Rate: <b>{rate.toFixed(1)}</b> events/min</span>
      {age !== null && age > 10 * 60_000 && <span className="stale">No events for a while. Check the CV service.</span>}
    </div>
  );
}
