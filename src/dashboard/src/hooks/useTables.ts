import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { getTableLayout, getTableStatus } from "../api/client";
import { mergeTables } from "../lib/tables";

/**
 * GET /tables (geometry, rarely changes) + GET /tables/status (live state).
 * The WebSocket patches the status cache directly; the 30 s poll is only a safety net.
 */
export function useTables() {
  const layout = useQuery({ queryKey: ["tables", "layout"], queryFn: getTableLayout, staleTime: 5 * 60_000 });
  const status = useQuery({ queryKey: ["tables", "status"], queryFn: getTableStatus, refetchInterval: 30_000 });
  const tables = useMemo(() => mergeTables(layout.data ?? [], status.data), [layout.data, status.data]);
  return {
    tables,
    layoutLoaded: !!layout.data,
    isLoading: layout.isLoading || (status.isLoading && !layout.data),
    layoutError: layout.error as Error | null,
    statusError: status.isError ? (status.error as Error) : null,
    refetch: () => { layout.refetch(); status.refetch(); },
    isFetching: layout.isFetching || status.isFetching,
  };
}
