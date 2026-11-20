import { useMemo } from "react";
import { useNow } from "../hooks/useNow";
import { buildAlerts } from "../lib/tables";
import type { TableView } from "../types";
import { IconAlert } from "./Icons";

interface Props { tables: TableView[]; longMin: number; entrance?: number; hall?: number; hallAt?: number }

/** What needs a manager's eyes right now. Shows nothing until there is data, and one quiet line when all is well. */
export function Attention({ tables, longMin, entrance, hall, hallAt }: Props) {
  const now = useNow(5_000);
  const alerts = useMemo(() => buildAlerts({ tables, now, longMin, entrance, hall, hallAt }), [tables, now, longMin, entrance, hall, hallAt]);
  if (entrance === undefined && hall === undefined && !tables.length) return null;
  if (!alerts.length) return <p className="att-ok" role="status">Nothing needs attention right now.</p>;
  return (
    <section className="att" aria-label="Needs attention">
      <h2>Needs attention <em className="pill">{alerts.length}</em></h2>
      <ul>
        {alerts.map((a) => (
          <li key={a.id} className={`att-item ${a.level}`} role={a.level === "bad" ? "alert" : undefined}>
            <IconAlert />
            <div><b>{a.title}</b><span>{a.detail}</span></div>
          </li>
        ))}
      </ul>
    </section>
  );
}
