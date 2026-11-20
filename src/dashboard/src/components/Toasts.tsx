import { useEffect, useRef, useState } from "react";
import type { TableView } from "../types";

interface Toast { key: number; tone: "free" | "occupied"; text: string }

/** Small notices when a table changes state. The first load of the statuses is not announced. */
export function TableToasts({ tables }: { tables: TableView[] }) {
  const prev = useRef<Record<string, string>>();
  const seq = useRef(0);
  const [items, setItems] = useState<Toast[]>([]);

  useEffect(() => {
    const cur: Record<string, string> = {};
    for (const t of tables) if (t.status !== "unknown") cur[t.id] = t.status;
    const before = prev.current;
    prev.current = cur;
    if (!before) return;
    const fresh: Toast[] = [];
    for (const t of tables) {
      if (t.status === "unknown" || !before[t.id] || before[t.id] === t.status) continue;
      fresh.push({
        key: ++seq.current, tone: t.status,
        text: t.status === "occupied" ? `${t.name} is now occupied${t.people ? ` by ${t.people}` : ""}` : `${t.name} is now free`,
      });
    }
    if (!fresh.length) return;
    setItems((p) => [...p, ...fresh].slice(-4));
    for (const f of fresh) setTimeout(() => setItems((p) => p.filter((x) => x.key !== f.key)), 5000);
  }, [tables]);

  return (
    <div className="toasts" aria-live="polite">
      {items.map((i) => <div key={i.key} className={`toast ${i.tone}`}>{i.text}</div>)}
    </div>
  );
}
