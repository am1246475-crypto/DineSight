import { fmtMin, type Analytics } from "../lib/analytics";

export function Insights({ a }: { a: Analytics }) {
  const issues: string[] = [];
  if (a.min < 0) issues.push("الخروج أكبر من الدخول في الفترة (الإشغال نزل تحت الصفر): غالباً دخول لم يُعدّ أو تتبع أعاد ترقيم الشخص.");
  if (a.flicker.length) issues.push(`${a.flicker.length} شخص عبروا الخط 4 مرات أو أكثر: ممكن تذبذب في الـ tracking أو في خط العدّ.`);
  return (
    <div className="insights">
      <p className="dwell">وقت البقاء: متوسط <b>{fmtMin(a.dwell.avgMin)}</b> · وسيط <b>{fmtMin(a.dwell.medianMin)}</b> (تقدير من تطابق دخول وخروج نفس الـ track_id)</p>
      {issues.length === 0
        ? <p className="ok">لا توجد مؤشرات مشاكل في جودة العدّ.</p>
        : <ul className="issues">{issues.map((t) => <li key={t}>{t}</li>)}</ul>}
      {a.flicker.length > 0 && (
        <p className="muted">أكثر track_id عبوراً: {a.flicker.slice(0, 5).map((f) => `#${f.track} (${f.count})`).join("، ")}</p>
      )}
    </div>
  );
}
