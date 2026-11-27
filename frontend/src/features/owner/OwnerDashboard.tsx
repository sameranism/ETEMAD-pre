import { useEffect, useRef, useState } from "react";
import Settings from "../settings/Settings";
import SourcesManager, { ForeignMonitor, parseSources } from "../sources/SourcesManager";
import PublishingCenter from "../publishing/PublishingCenter";
import { object, parseControls, parseOwner, request, RequestError, timeFa, useResource,
  type Controls, type OwnerData } from "../dashboard/data";

const OUTPUT_NAMES: Record<string, string> = {
  public_analysis: "تحلیل عمومی", studio: "استودیو", studio_audio: "صدای استودیو", studio_video: "ویدئوی استودیو",
  publishing: "انتشار", publishing_domestic: "انتشار داخلی", publishing_foreign: "انتشار خارجی",
  publishing_reports: "انتشار گزارش عمومی",
};
const STATE_NAMES: Record<string, string> = { queued: "صف", running: "در اجرا", held: "متوقف",
  failed: "ناموفق", done: "اتمام", cancelled: "لغو" };
const DB_NAMES: Record<string,string> = {central:"مرکزی",domestic:"داخلی",foreign:"خارجی"};
const MONITOR_NAMES: Record<string, string> = { domestic: "پایش منابع داخلی", foreign: "پایش منابع خارجی" };
const MONITOR_STATE: Record<string, string> = {
  ok: "در دسترس", unavailable: "در دسترس نیست", not_authorized: "خارج از مجوز",
};
function OutputControls({ token, controls, refresh }: { token: string; controls: Controls; refresh: () => void }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [change, setChange] = useState<{ key: string; enabled: boolean } | null>(null);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  async function save() {
    if (active.current || !change || !controls.can_update) return;
    const controller = new AbortController(); active.current = controller; setBusy(true); setMessage("");
    try {
      const result = await request(token, "/v1/ui/owner-dashboard/output-controls", controller.signal,
        "PUT", { ...change, expected_revision: controls.revision });
      if (!object(result) || result.schema_version !== "1.0") throw new Error("invalid control");
      const next = parseControls(result.controls);
      if (next.revision < controls.revision + 1) throw new Error("invalid revision");
      if (!controller.signal.aborted) { setChange(null); refresh(); }
    } catch (e) {
      if (!controller.signal.aborted) {
        setChange(null);
        setMessage(e instanceof RequestError && e.status === 409
          ? "نسخه تنظیمات تغییر کرده است؛ پیش از تغییر دوباره آن را دریافت کنید." : "تغییر ثبت نشد؛ وضعیت را دوباره دریافت کنید.");
      }
    } finally { if (active.current === controller) active.current = null; if (!controller.signal.aborted) setBusy(false); }
  }
  return <section className="dash-panel">
    <h2>کلیدهای مستقل خروجی</h2><p className="dash-note">نسخه {controls.revision.toLocaleString("fa-IR")} · وضعیت ثبت‌شده سمت سرور، نه تضمین فعال بودن سرویس خارجی.</p>
    <div className="dash-control-grid">{Object.entries(OUTPUT_NAMES).map(([key, title]) =>
      <div key={key} className="dash-control"><span>{title}</span><strong>{controls.values[key] ? "روشن" : "خاموش"}</strong>
      {controls.can_update && <button type="button" disabled={busy || !!message}
        onClick={() => setChange({ key, enabled: !controls.values[key] })}>تغییر</button>}</div>)}</div>
    {change && <div role="group" aria-label="تأیید تغییر خروجی" className="dash-confirm">
      <p>{OUTPUT_NAMES[change.key]} {change.enabled ? "روشن" : "خاموش"} شود؟</p>
      <button type="button" disabled={busy} onClick={() => void save()}>ثبت تغییر</button>
      <button type="button" disabled={busy} onClick={() => setChange(null)}>لغو</button></div>}
    {message && <p role="alert">{message} <button type="button" onClick={refresh}>دریافت نسخه جاری</button></p>}
  </section>;
}
function SourceRegistry({ token }: { token: string }) {
  const q = useResource(token, "/v1/sources", parseSources);
  return <SourcesManager sources={q.data ?? []} state={q.loading ? "loading" : q.error === 403 ? "forbidden" : q.error !== null ? "error" : "ready"} />;
}
export function OwnerOverview({ data }: { data: OwnerData }) {
  return <>
    {data.health && <section className="dash-panel"><h2>سلامت زیرساخت</h2><div className="dash-kpis">
      {Object.entries(data.health).map(([key, value]) => <div key={key}><span>{DB_NAMES[key] ?? key}</span>
        <strong>{value === "ok" ? "متصل" : "در دسترس نیست"}</strong></div>)}</div>
      <p className="dash-note">آزمون اتصال پایگاه داده؛ نشان‌دهنده سلامت کامل سامانه نیست.</p>
      {data.workflow_counts && <div className="dash-control-grid">{Object.entries(data.workflow_counts).map(([k, v]) =>
        <div className="dash-control" key={k}><span>{STATE_NAMES[k] ?? k}</span><strong>{v.toLocaleString("fa-IR")}</strong></div>)}</div>}
    </section>}
    {data.ai_cost && <section className="dash-panel"><h2>مصرف مدل‌ها</h2><p>ماه UTC: <span dir="ltr">{data.ai_cost.period}</span></p>
      <strong className="dash-value">{data.ai_cost.committed_usd === null ? "داده موجود نیست" : data.ai_cost.committed_usd.toLocaleString("fa-IR", { maximumFractionDigits: 6 }) + " دلار"}</strong>
      <p className="dash-note">برآورد دفتر هزینه شامل رزروها و هزینه نامطمئن؛ صورتحساب نهایی نیست.</p></section>}
    {data.sources && <section className="dash-panel"><h2>سلامت منابع</h2>
      <p>{data.sources.registered.toLocaleString("fa-IR")} ثبت‌شده · {data.sources.enabled.toLocaleString("fa-IR")} فعال</p>
      <div className="dash-control-grid">{Object.entries(data.sources.monitoring).map(([stream, status]) =>
        <div className="dash-control" key={stream}><span>{MONITOR_NAMES[stream]}</span>
          <strong>{MONITOR_STATE[status]}</strong></div>)}</div>
      <div className="dash-table"><table><thead><tr><th>منبع</th><th>جریان</th><th>فعال</th><th>آخرین دریافت موفق</th><th>خطاهای متوالی</th></tr></thead>
      <tbody>{data.sources.items.map(s => <tr key={s.source_id}><td dir="ltr">{s.source_id}</td>
        <td>{s.stream === "domestic" ? "داخلی" : "خارجی"}</td><td>{s.enabled ? "بله" : "خیر"}</td>
        <td>{s.last_success_at ? timeFa(s.last_success_at) : "ثبت نشده"}</td>
        <td>{s.consecutive_failures?.toLocaleString("fa-IR") ?? "نامشخص"}</td></tr>)}</tbody></table></div>
      <p className="dash-note">حداکثر ۲۰۰ منبع؛ وضعیت ثبت‌نشده به معنی سالم بودن نیست.</p></section>}
    {data.audit && <section className="dash-panel"><h2>رخدادهای عملیاتی اخیر</h2>
      <div className="dash-table"><table><thead><tr><th>زمان تهران</th><th>نقش</th><th>عملیات</th><th>نتیجه</th></tr></thead>
      <tbody>{data.audit.map((a, i) => <tr key={i}><td>{timeFa(a.occurred_at)}</td><td dir="ltr">{a.actor_role}</td>
        <td dir="ltr">{a.action}</td><td dir="ltr">{a.decision}</td></tr>)}</tbody></table></div></section>}
  </>;
}
export default function OwnerDashboard({ token }: { token: string }) {
  const q = useResource(token, "/v1/ui/owner-dashboard", parseOwner);
  return <div className="role-dashboard owner-dashboard">
    <header className="dash-header"><div><p className="eyebrow">دیدبان · عملیات فنی</p><h1>داشبورد مالک سامانه</h1>
      <p>زیرساخت، مصرف، کنترل خروجی و منابع در محدوده مجوز فنی</p></div><button type="button" onClick={q.refresh}>به‌روزرسانی</button></header>
    {q.loading && <p role="status">در حال دریافت وضعیت…</p>}
    {q.error !== null && <p role="alert">داشبورد فنی در دسترس نیست.</p>}
    {q.data && <>
      {!Object.keys(q.data).length && <p className="message">برای هیچ بخش فنی مجوز ثبت نشده است.</p>}
      <OwnerOverview data={q.data} />
      {q.data.controls && <OutputControls key={q.data.controls.revision} token={token} controls={q.data.controls} refresh={q.refresh} />}
      {q.data.sources && <SourceRegistry token={token} />}
      {q.data.foreign_monitor && <ForeignMonitor token={token} />}
      {q.data.schedule && <Settings token={token} />}
      {q.data.publishing && <PublishingCenter token={token} />}
    </>}
  </div>;
}
