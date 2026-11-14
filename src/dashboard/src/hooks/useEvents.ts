import { useQuery } from "@tanstack/react-query";
import { getEvents } from "../api/client";
import type { EventsQuery } from "../types";

export const useEvents = (filter: EventsQuery, paused: boolean) =>
  useQuery({
    queryKey: ["events", filter],
    queryFn: () => getEvents(filter),
    refetchInterval: paused ? false : 15_000,
    placeholderData: (prev) => prev,
  });
