import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { connectLive, getOccupancy, type LiveConnection } from "../api/client";
import { appendPoint, patchStates } from "../lib/tables";
import type { ConnectionStatus, Occupancy, RoomCountPoint, TableState } from "../types";

export interface Retry { attempt: number; at: number /* epoch ms of the next attempt */ }

/**
 * One WebSocket (/ws/live) for the whole app:
 *  - "occupancy"  -> entrance counters (React state)
 *  - "room_count" -> hall-camera count (React state + appended to the history chart cache)
 *  - "table"      -> patched straight into the /tables/status cache, so the map and list update instantly
 * After a reconnect the REST data is refetched, because messages sent while offline are lost.
 */
export function useLive() {
  const qc = useQueryClient();
  const initial = useQuery({ queryKey: ["occupancy"], queryFn: getOccupancy });
  const [live, setLive] = useState<Occupancy>();
  const [room, setRoom] = useState<RoomCountPoint>();
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [retry, setRetry] = useState<Retry>();
  const conn = useRef<LiveConnection>();
  const wasOffline = useRef(false);

  useEffect(() => {
    conn.current = connectLive(
      (m) => {
        if (m.type === "occupancy") setLive({ current: m.current, entered: m.entered, exited: m.exited, updated_at: new Date().toISOString() });
        else if (m.type === "room_count") {
          const p = { t: m.t, count: m.count };
          setRoom((prev) => (prev && prev.t > p.t ? prev : p));
          qc.setQueryData<RoomCountPoint[]>(["room-count", "history"], (prev) => (prev ? appendPoint(prev, p) : prev));
        } else {
          qc.setQueryData<TableState[]>(["tables", "status"], (prev) => (prev ? patchStates(prev, m.tables) : prev));
        }
      },
      (s) => {
        setStatus(s);
        if (s === "offline") wasOffline.current = true;
        if (s === "live") {
          setRetry(undefined);
          if (wasOffline.current) { // back online: resync everything that may have changed meanwhile
            wasOffline.current = false;
            qc.invalidateQueries({ queryKey: ["occupancy"] });
            qc.invalidateQueries({ queryKey: ["tables", "status"] });
            qc.invalidateQueries({ queryKey: ["room-count"] });
            qc.invalidateQueries({ queryKey: ["events"] });
          }
        }
      },
      (attempt, delay) => setRetry({ attempt, at: Date.now() + delay }),
    );
    return () => conn.current?.close();
  }, [qc]);

  const reconnect = useCallback(() => conn.current?.reconnect(), []);
  return { occupancy: live ?? initial.data, room, status, retry, reconnect, error: initial.error };
}
