import { ago } from "../lib/analytics";
import type { CountEvent } from "../types";
import { IconIn, IconOut } from "./Icons";

/** Recent events بشكل مختصر ومقروء (أحدث 6). */
export function RecentFeed({ events }: { events: CountEvent[] }) {
  const latest = [...events].sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp)).slice(0, 6);
  return (
    <section className="card">
      <div className="sec-head"><h2>Recent events</h2></div>
      {latest.length === 0 ? <p className="empty">No events yet.</p> : (
        <ul className="feed">
          {latest.map((e, i) => (
            <li key={e.id ?? `${e.timestamp}-${i}`} className={e.event_type}>
              <span className="feed-ico">{e.event_type === "enter" ? <IconIn /> : <IconOut />}</span>
              <span className="feed-txt"><b>{e.event_type === "enter" ? "Enter" : "Exit"} · {e.camera_id} #{e.track_id}</b></span>
              <small className="feed-time">{ago(Date.now() - Date.parse(e.timestamp))} ago</small>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
