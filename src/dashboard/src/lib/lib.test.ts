import { describe, expect, it } from "vitest";
import { analyze, fmtMin, toCsv } from "./analytics";
import { normalizeEvents } from "./normalize";
import type { CountEvent } from "../types";

const t0 = Date.parse("2026-11-14T10:00:00Z");
const ev = (track: number, type: "enter" | "exit", min: number): CountEvent => ({
  camera_id: "entrance", track_id: track, event_type: type,
  timestamp: new Date(t0 + min * 60_000).toISOString(), meta: { anchor: [190, 300] },
});

describe("analyze", () => {
  const events = [ev(1, "enter", 0), ev(2, "enter", 1), ev(1, "exit", 10)];

  it("anchors the occupancy curve on the current value", () => {
    const a = analyze(events, 5);
    expect(a.series.map((p) => p.v)).toEqual([4, 5, 6, 5, 5]); // baseline, 3 events, final point
    expect(a.peak).toBe(6);
    expect(a.min).toBe(4);
  });
  it("computes stay time from enter/exit of the same track", () => {
    const a = analyze(events, 5);
    expect(a.dwell.n).toBe(1);
    expect(a.dwell.avgMin).toBeCloseTo(10);
  });
  it("flags tracks crossing 4+ times", () => {
    const flick = [ev(7, "enter", 0), ev(7, "exit", 1), ev(7, "enter", 2), ev(7, "exit", 3)];
    expect(analyze(flick, 0).flicker).toEqual([{ track: 7, count: 4 }]);
  });
  it("reports min < 0 when exits exceed entries", () => {
    const a = analyze([ev(1, "exit", 0), ev(2, "exit", 1), ev(3, "enter", 2)], 0);
    expect(a.min).toBe(-1); // baseline 1 -> 0 -> -1 -> 0
  });
  it("handles empty input", () => {
    const a = analyze([], 3);
    expect(a.series).toEqual([]);
    expect(a.dwell.avgMin).toBeNull();
  });
});

describe("normalizeEvents", () => {
  it("accepts ts as an alias and drops malformed rows", () => {
    const { events, dropped } = normalizeEvents([
      { camera_id: "entrance", track_id: 1, event_type: "enter", ts: "2026-11-14T10:00:00Z", meta: {} },
      { camera_id: "entrance", track_id: 2, event_type: "wave", timestamp: "2026-11-14T10:00:00Z" },
      { camera_id: "entrance", track_id: 3, event_type: "exit", timestamp: "not-a-date" },
    ]);
    expect(events).toHaveLength(1);
    expect(events[0].timestamp).toBe("2026-11-14T10:00:00Z");
    expect(dropped).toBe(2);
  });
});

describe("formatting", () => {
  it("formats minutes", () => {
    expect(fmtMin(null)).toBe("–");
    expect(fmtMin(90)).toBe("1 س 30 د");
  });
  it("exports CSV with a BOM header", () => {
    const csv = toCsv([ev(1, "enter", 0)]);
    expect(csv.startsWith("\uFEFFid,camera_id")).toBe(true);
    expect(csv).toContain("entrance,1,enter");
  });
});
