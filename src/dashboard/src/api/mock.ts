import { config } from "../config";
import type { CountEvent, EventsQuery, EventType, LiveMessage, Occupancy } from "../types";

const state = { entered: 0, exited: 0, events: [] as CountEvent[] }; // events: newest first
const inside: number[] = [];
let seq = 0, tid = 0;

const makeEvent = (type: EventType, at: number): CountEvent => {
  let track: number;
  if (type === "enter") { track = ++tid; inside.push(track); }
  else track = inside.length ? inside.splice(Math.floor(Math.random() * inside.length), 1)[0] : ++tid;
  const { w, h } = config.frame;
  return {
    id: ++seq,
    camera_id: Math.random() < 0.85 ? "entrance" : "exit",
    track_id: track,
    event_type: type,
    timestamp: new Date(at).toISOString(),
    meta: { frame: Math.floor(at / 40) % 100000,
            anchor: [Math.round(w * config.lineX + (Math.random() - 0.5) * 30), Math.round(h * (0.35 + Math.random() * 0.6))] },
  };
};

function record(type: EventType, at: number) {
  state.events.unshift(makeEvent(type, at));
  type === "enter" ? state.entered++ : state.exited++;
}

(function seed() {
  const now = Date.now(), span = 14 * 3600_000;
  let t = now - span;
  while (t < now - 60_000) {
    t += Math.random() * (span / 150) * 2;
    const run = state.entered - state.exited;
    record(run <= 0 || Math.random() < 0.56 ? "enter" : "exit", Math.min(t, now - 30_000));
  }
})();

export const mockOccupancy = (): Occupancy => ({
  current: Math.max(state.entered - state.exited, 0), entered: state.entered, exited: state.exited,
  updated_at: state.events[0]?.timestamp ?? null,
});

export const mockEvents = ({ from, to, cameraId }: EventsQuery): CountEvent[] =>
  state.events.filter((e) =>
    (!cameraId || e.camera_id === cameraId) && (!from || e.timestamp >= from) && (!to || e.timestamp <= to));

export function mockLive(onMsg: (m: LiveMessage) => void): () => void {
  const id = setInterval(() => {
    const run = state.entered - state.exited;
    record(run <= 0 || Math.random() > 0.45 ? "enter" : "exit", Date.now());
    const o = mockOccupancy();
    onMsg({ type: "occupancy", current: o.current, entered: o.entered, exited: o.exited });
  }, 3000);
  return () => clearInterval(id);
}
