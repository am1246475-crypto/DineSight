import { Area, AreaChart, Bar, BarChart, CartesianGrid, Legend, ReferenceLine, ResponsiveContainer,
         Scatter, ScatterChart, Tooltip, XAxis, YAxis } from "recharts";
import { config } from "../config";
import type { Analytics } from "../lib/analytics";

const tip = { contentStyle: { background: "var(--surface)", border: "1px solid var(--line)", borderRadius: 8, color: "var(--ink)" } };
const hm = (t: number) => new Date(t).toLocaleTimeString("ar-EG", { hour: "2-digit", minute: "2-digit" });
const Empty = ({ text }: { text: string }) => <p className="empty">{text}</p>;

export function OccupancyChart({ series }: { series: Analytics["series"] }) {
  if (series.length < 2) return <Empty text="لا توجد أحداث في هذه الفترة." />;
  return (
    <div className="chart"><ResponsiveContainer width="100%" height={240}>
      <AreaChart data={series}>
        <CartesianGrid stroke="var(--line)" vertical={false} />
        <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={hm} stroke="var(--muted)" fontSize={11} />
        <YAxis stroke="var(--muted)" fontSize={11} allowDecimals={false} width={30} />
        <Tooltip {...tip} labelFormatter={hm} formatter={(v) => [v, "الإشغال"]} />
        <Area type="stepAfter" dataKey="v" stroke="var(--accent)" fill="var(--accent)" fillOpacity={0.15} strokeWidth={2} isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer></div>
  );
}

export function HourlyChart({ data }: { data: Analytics["hourly"] }) {
  if (!data.some((d) => d.enter || d.exit)) return <Empty text="لا توجد بيانات." />;
  return (
    <div className="chart"><ResponsiveContainer width="100%" height={240}>
      <BarChart data={data}>
        <CartesianGrid stroke="var(--line)" vertical={false} />
        <XAxis dataKey="hour" stroke="var(--muted)" fontSize={11} interval={2} />
        <YAxis stroke="var(--muted)" fontSize={11} allowDecimals={false} width={30} />
        <Tooltip {...tip} cursor={{ fill: "var(--line)", opacity: 0.4 }} />
        <Legend formatter={(v) => (v === "enter" ? "دخول" : "خروج")} />
        <Bar dataKey="enter" fill="var(--enter)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
        <Bar dataKey="exit" fill="var(--exit)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer></div>
  );
}

export function CamerasPanel({ cameras }: { cameras: Analytics["cameras"] }) {
  if (!cameras.length) return <Empty text="لا توجد بيانات." />;
  const max = Math.max(...cameras.map((c) => c.enter + c.exit), 1);
  return (
    <ul className="cams">
      {cameras.map((c) => (
        <li key={c.id}>
          <div><b>{c.id}</b><span>{c.enter} دخول · {c.exit} خروج</span></div>
          <div className="bar"><i className="e" style={{ width: `${(c.enter / max) * 100}%` }} /><i className="x" style={{ width: `${(c.exit / max) * 100}%` }} /></div>
        </li>
      ))}
    </ul>
  );
}

/** Where people cross the virtual line (meta.anchor, in frame pixels): helps you tune the line. */
export function CrossingMap({ points }: { points: Analytics["crossings"] }) {
  if (!points.length) return <Empty text="لا توجد نقاط عبور (meta.anchor)." />;
  const { w, h } = config.frame;
  return (
    <div className="chart"><ResponsiveContainer width="100%" height={240}>
      <ScatterChart margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid stroke="var(--line)" />
        <XAxis type="number" dataKey="x" domain={[0, w]} stroke="var(--muted)" fontSize={11} />
        <YAxis type="number" dataKey="y" domain={[0, h]} reversed stroke="var(--muted)" fontSize={11} width={30} />
        <ReferenceLine x={w * config.lineX} stroke="var(--warn)" strokeDasharray="4 4" />
        <Tooltip {...tip} cursor={{ strokeDasharray: "3 3" }} />
        <Scatter data={points.filter((p) => p.type === "enter")} fill="var(--enter)" fillOpacity={0.7} isAnimationActive={false} />
        <Scatter data={points.filter((p) => p.type === "exit")} fill="var(--exit)" fillOpacity={0.7} isAnimationActive={false} />
      </ScatterChart>
    </ResponsiveContainer></div>
  );
}
