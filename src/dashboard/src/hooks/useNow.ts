import { useEffect, useState } from "react";

/** Re-renders every `ms` so durations such as "12 minutes" keep counting without any server message. */
export function useNow(ms = 10_000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), ms); return () => clearInterval(id); }, [ms]);
  return now;
}
