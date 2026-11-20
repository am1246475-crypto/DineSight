import { useQuery } from "@tanstack/react-query";
import { getRoomHistory } from "../api/client";

/** GET /room-count/history. Live "room_count" messages are appended to this same cache by useLive. */
export const useRoomHistory = () =>
  useQuery({ queryKey: ["room-count", "history"], queryFn: getRoomHistory, refetchInterval: 60_000 });
