import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { connectLive, getOccupancy } from "../api/client";
import type { ConnectionStatus, Occupancy } from "../types";

export function useLiveOccupancy() {
  const initial = useQuery({ queryKey: ["occupancy"], queryFn: getOccupancy });
  const [live, setLive] = useState<Occupancy>();
  const [status, setStatus] = useState<ConnectionStatus>("connecting");

  useEffect(
    () => connectLive((m) => setLive({ current: m.current, entered: m.entered, exited: m.exited,
                                       updated_at: new Date().toISOString() }), setStatus),
    [],
  );
  return { occupancy: live ?? initial.data, status, error: initial.error };
}
