import type { ReactNode } from "react";
import { discrepancy } from "../lib/tables";
import type { ConnectionStatus, Occupancy, RoomCountPoint } from "../types";
import { useCountUp } from "./Widgets";
import { Skel, useFlash } from "./Kpis";
import { IconCam, IconIn, IconOut, IconUsers } from "./Icons";
import { useNow } from "../hooks/useNow";

const clock = (t: number) => new Date(t).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function Card({ icon, label, source, badge, tone, value, loading, foot }: {
  icon: ReactNode; label: string; source: string; badge?: string; tone: string;
  value?: number; loading: boolean; foot?: ReactNode;
}) {
  const shown = useCountUp(value);
  const flash = useFlash(value);
  return (
    <div className={`card oc t-${tone}`}>
      <span className="k-label">{icon}{label}{badge && <em className={`oc-badge ${tone}`}>{badge}</em>}</span>
      {value !== undefined ? <b className={`oc-num ${flash ? "flash" : ""}`}>{shown}</b> : loading ? <Skel big /> : <b className="oc-num none">–</b>}
      <small className="oc-src">{source}</small>
      {foot && <small className="oc-foot">{foot}</small>}
    </div>
  );
}

/**
 * Four counters. Two of them answer the same question in different ways and must never be confused:
 *  - People Inside: what the hall camera actually sees right now.
 *  - Entry Counter: what the entrance camera computed (entered - exited).
 */
export function OccupancyCards({ occ, room, status }: { occ?: Occupancy; room?: RoomCountPoint; status: ConnectionStatus }) {
  const now = useNow(1000);
  const d = discrepancy(occ?.current, room?.count);
  const loading = status === "connecting";
  const age = room ? Math.max(0, Math.round((now - room.t) / 1000)) : null;
  return (
    <section aria-label="Occupancy" className="oc-wrap">
      <div className="oc-grid">
        <Card tone="inside" icon={<IconUsers />} label="People Inside" badge="Actual" value={room?.count} loading={loading}
              source="Hall camera · people visible now"
              foot={room ? `Updated ${age! < 60 ? `${age}s` : `${Math.floor(age! / 60)}m`} ago (${clock(room.t)})` : loading ? undefined : "Waiting for the hall camera"} />
        <Card tone="entry" icon={<IconCam />} label="Entry Counter" badge="Computed" value={occ?.current} loading={loading}
              source="Entrance camera · entered − exited" />
        <Card tone="enter" icon={<IconIn />} label="Total Entered" value={occ?.entered} loading={loading} source="Entrance camera · since start" />
        <Card tone="exit" icon={<IconOut />} label="Total Exited" value={occ?.exited} loading={loading} source="Entrance camera · since start" />
      </div>
      {d && (
        <p className={`oc-diff ${d.level}`} role="status">
          <b>{d.level === "match" ? "Counts match" : `Difference: ${d.diff > 0 ? "+" : ""}${d.diff}`}</b>
          <span>
            {d.level === "match" && "The entrance counter and the hall camera agree."}
            {d.diff > 0 && `The entrance counter says ${Math.abs(d.diff)} more than the hall camera sees. Some people may be outside the camera view, or an exit was missed.`}
            {d.diff < 0 && `The hall camera sees ${Math.abs(d.diff)} more than the entrance counter. An entry may have been missed.`}
          </span>
        </p>
      )}
    </section>
  );
}
