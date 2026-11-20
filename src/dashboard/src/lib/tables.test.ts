import { describe, expect, it } from "vitest";
import { normalizeRoomHistory, normalizeTableLayout, normalizeTableState, normalizeTableStatus, parseLiveMessage, parsePolygon } from "./normalize";
import { appendPoint, bbox, buildAlerts, centroid, discrepancy, fmtDuration, mergeTables, patchStates, tablesToCsv, tableStats, toPoints } from "./tables";
import type { TableView } from "../types";

const NOW = Date.parse("2026-11-17T12:00:00Z");

describe("fmtDuration", () => {
  it("formats minutes and hours in plain English", () => {
    expect(fmtDuration(20_000)).toBe("less than a minute");
    expect(fmtDuration(60_000)).toBe("1 minute");
    expect(fmtDuration(12 * 60_000 + 59_000)).toBe("12 minutes");
    expect(fmtDuration(60 * 60_000)).toBe("1 hour");
    expect(fmtDuration(65 * 60_000)).toBe("1 hour 5 minutes");
    expect(fmtDuration(150 * 60_000)).toBe("2 hours 30 minutes");
    expect(fmtDuration(-5)).toBe("less than a minute");
  });
});

describe("polygon helpers", () => {
  const sq: [number, number][] = [[0.2, 0.2], [0.4, 0.2], [0.4, 0.6], [0.2, 0.6]];
  it("centroid of a rectangle is its centre; also for clockwise/counter-clockwise order", () => {
    const [x, y] = centroid(sq);
    expect(x).toBeCloseTo(0.3); expect(y).toBeCloseTo(0.4);
    const [x2, y2] = centroid([...sq].reverse());
    expect(x2).toBeCloseTo(0.3); expect(y2).toBeCloseTo(0.4);
  });
  it("degenerate polygons fall back to the bounding-box centre", () => {
    const [x, y] = centroid([[0.1, 0.1], [0.3, 0.1], [0.5, 0.1]]);
    expect(x).toBeCloseTo(0.3); expect(y).toBeCloseTo(0.1);
  });
  it("scales 0..1 to the viewBox", () => {
    expect(toPoints(sq, 1000, 500)).toBe("200,100 400,100 400,300 200,300");
    expect(bbox(sq).w).toBeCloseTo(0.2);
  });
  it("parsePolygon accepts pairs and {x,y}, clamps to 0..1, rejects < 3 points", () => {
    expect(parsePolygon([[0, 0], [1, 0], [1, 1]])).toEqual([[0, 0], [1, 0], [1, 1]]);
    expect(parsePolygon([{ x: 0.1, y: 0.2 }, { x: 0.3, y: 0.2 }, { x: 2, y: -1 }])).toEqual([[0.1, 0.2], [0.3, 0.2], [1, 0]]);
    expect(parsePolygon([[0, 0], [1, 1]])).toBeNull();
    expect(parsePolygon([[0, 0], [1, 1], ["a", 2]])).toBeNull();
    expect(parsePolygon("x")).toBeNull();
  });
});

describe("normalizeTableLayout", () => {
  it("accepts id/name/polygon aliases and drops broken rows", () => {
    const { tables, dropped } = normalizeTableLayout([
      { id: "T1", name: "Window 1", polygon: [[0, 0], [0.1, 0], [0.1, 0.1]] },
      { table_id: 2, points: [{ x: 0, y: 0 }, { x: 0.2, y: 0 }, { x: 0.2, y: 0.2 }] },
      { id: "bad", polygon: [[0, 0]] },
      null,
    ]);
    expect(tables.map((t) => [t.id, t.name])).toEqual([["T1", "Window 1"], ["2", "2"]]);
    expect(dropped).toBe(2);
  });
  it("accepts {tables: [...]} and returns nothing for garbage", () => {
    expect(normalizeTableLayout({ tables: [{ id: "A", polygon: [[0, 0], [1, 0], [1, 1]] }] }).tables).toHaveLength(1);
    expect(normalizeTableLayout("nope").tables).toEqual([]);
  });
});

describe("normalizeTableState", () => {
  it("reads status + people + since", () => {
    expect(normalizeTableState({ table_id: "T1", status: "occupied", people_count: 3, occupied_since: "2026-11-17T11:48:00Z" }, NOW))
      .toEqual({ id: "T1", status: "occupied", people: 3, since: "2026-11-17T11:48:00.000Z" });
  });
  it("turns a relative duration into an absolute since", () => {
    const s = normalizeTableState({ id: "T2", occupied: true, people: 2, duration_minutes: 12 }, NOW)!;
    expect(NOW - Date.parse(s.since!)).toBe(12 * 60_000);
    const s2 = normalizeTableState({ id: "T2", status: "occupied", duration_seconds: 90 }, NOW)!;
    expect(NOW - Date.parse(s2.since!)).toBe(90_000);
  });
  it("free tables have no since; unknown statuses count as free; no id -> null", () => {
    expect(normalizeTableState({ id: "T3", status: "free", people_count: 0, since: "2026-11-17T11:00:00Z" }, NOW))
      .toEqual({ id: "T3", status: "free", people: 0, since: null });
    expect(normalizeTableState({ id: "T4", status: "whatever" }, NOW)!.status).toBe("free");
    expect(normalizeTableState({ status: "occupied" }, NOW)).toBeNull();
  });
  it("normalizeTableStatus counts dropped rows", () => {
    const r = normalizeTableStatus([{ id: "A", status: "occupied", people: 1 }, {}]);
    expect(r.states).toHaveLength(1); expect(r.dropped).toBe(1);
  });
});

describe("normalizeRoomHistory", () => {
  it("accepts several shapes, sorts by time and drops bad samples", () => {
    const { points, dropped } = normalizeRoomHistory([
      { timestamp: "2026-11-17T10:05:00Z", count: 7 },
      { ts: "2026-11-17T10:00:00Z", room_count: 5.4 },
      ["2026-11-17T10:10:00Z", 9],
      { timestamp: "nope", count: 1 },
      { timestamp: "2026-11-17T10:15:00Z" },
    ]);
    expect(points.map((p) => p.count)).toEqual([5, 7, 9]);
    expect(dropped).toBe(2);
    expect(normalizeRoomHistory({ history: [{ time: 1_763_373_600, count: 2 }] }).points[0].t).toBe(1_763_373_600_000);
  });
});

describe("parseLiveMessage", () => {
  it("occupancy", () => {
    expect(parseLiveMessage({ type: "occupancy", current: 4, entered: 10, exited: 6 })).toEqual({ type: "occupancy", current: 4, entered: 10, exited: 6 });
    expect(parseLiveMessage({ type: "occupancy", current: 4 })).toBeNull();
  });
  it("room_count uses its timestamp, or the arrival time", () => {
    expect(parseLiveMessage({ type: "room_count", count: 8, timestamp: "2026-11-17T12:00:00Z" }, 1)).toEqual({ type: "room_count", count: 8, t: NOW });
    expect(parseLiveMessage({ type: "room_count", count: 8 }, 123)).toEqual({ type: "room_count", count: 8, t: 123 });
  });
  it("table: single or batch", () => {
    const one = parseLiveMessage({ type: "table", table_id: "T1", status: "free", people_count: 0 }, NOW);
    expect(one).toEqual({ type: "table", tables: [{ id: "T1", status: "free", people: 0, since: null }] });
    const many = parseLiveMessage({ type: "table", tables: [{ id: "A", status: "free" }, { id: "B", status: "occupied", people: 2 }] }, NOW);
    expect(many && many.type === "table" && many.tables.length).toBe(2);
  });
  it("ignores unknown or malformed frames", () => {
    expect(parseLiveMessage({ type: "ping" })).toBeNull();
    expect(parseLiveMessage("text")).toBeNull();
    expect(parseLiveMessage(null)).toBeNull();
  });
});

describe("state merging", () => {
  const layout = [{ id: "T1", name: "T1", polygon: [[0, 0], [1, 0], [1, 1]] as [number, number][] }, { id: "T2", name: "T2", polygon: [[0, 0], [1, 0], [1, 1]] as [number, number][] }];
  it("tables missing from the status list are free once status loaded, unknown before", () => {
    const m = mergeTables(layout, [{ id: "T1", status: "occupied", people: 2, since: "x" }]);
    expect(m.map((t) => t.status)).toEqual(["occupied", "free"]);
    expect(mergeTables(layout, undefined).map((t) => t.status)).toEqual(["unknown", "unknown"]);
  });
  it("patchStates replaces by id and keeps the rest", () => {
    const next = patchStates([{ id: "T1", status: "free", people: 0, since: null }, { id: "T2", status: "free", people: 0, since: null }],
      [{ id: "T2", status: "occupied", people: 3, since: "s" }]);
    expect(next.find((s) => s.id === "T2")!.people).toBe(3);
    expect(next).toHaveLength(2);
  });
  it("appendPoint keeps order, replaces equal timestamps, ignores stale ones and caps length", () => {
    const base = [{ t: 1, count: 1 }, { t: 2, count: 2 }];
    expect(appendPoint(base, { t: 3, count: 3 })).toHaveLength(3);
    expect(appendPoint(base, { t: 2, count: 9 })[1].count).toBe(9);
    expect(appendPoint(base, { t: 1, count: 5 })).toBe(base);
    expect(appendPoint(base, { t: 3, count: 3 }, 2).map((p) => p.t)).toEqual([2, 3]);
  });
});

describe("discrepancy", () => {
  it("compares the entrance counter with the hall camera", () => {
    expect(discrepancy(5, 5)).toEqual({ diff: 0, level: "match" });
    expect(discrepancy(7, 5)).toEqual({ diff: 2, level: "minor" });
    expect(discrepancy(5, 9)).toEqual({ diff: -4, level: "major" });
    expect(discrepancy(undefined, 5)).toBeNull();
  });
});

describe("alerts, stats and csv", () => {
  const now = Date.parse("2026-11-18T12:00:00Z");
  const mk = (id: string, mins: number | null, people = 2): TableView => ({
    id, name: id, polygon: [[0, 0], [1, 0], [1, 1]],
    status: mins === null ? "free" : "occupied", people: mins === null ? 0 : people,
    since: mins === null ? null : new Date(now - mins * 60_000).toISOString(),
  });
  it("flags only tables past the limit", () => {
    const a = buildAlerts({ tables: [mk("T1", 90), mk("T2", 20), mk("T3", null)], now, longMin: 60 });
    expect(a.map((x) => x.id)).toEqual(["long-T1"]);
  });
  it("flags a stale hall camera first and a big count gap", () => {
    const a = buildAlerts({ tables: [], now, longMin: 60, entrance: 20, hall: 10, hallAt: now - 120_000 });
    expect(a.map((x) => x.id)).toEqual(["stale", "gap"]);
  });
  it("computes average and longest stay", () => {
    const s = tableStats([mk("T1", 10), mk("T2", 30), mk("T3", null)], now);
    expect(s.avg).toBe(20 * 60_000);
    expect(s.longest?.name).toBe("T2");
    expect(tableStats([mk("T3", null)], now)).toEqual({ avg: null, longest: null });
  });
  it("exports one csv row per table", () => {
    const rows = tablesToCsv([mk("T1", 15), mk("T2", null)], now).split("\n");
    expect(rows).toHaveLength(3);
    expect(rows[1]).toBe('"T1","T1","occupied","2","2026-11-18T11:45:00.000Z","15"');
  });
});
