import { useState } from "react";

/** useState that survives reloads (localStorage, guarded: it can be unavailable). */
export function usePersisted<T>(key: string, initial: T) {
  const [value, setValue] = useState<T>(() => {
    try { const raw = localStorage.getItem(key); return raw === null ? initial : (JSON.parse(raw) as T); }
    catch { return initial; }
  });
  const set = (v: T) => { setValue(v); try { localStorage.setItem(key, JSON.stringify(v)); } catch { /* ignore */ } };
  return [value, set] as const;
}
