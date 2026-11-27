import { useEffect, useRef, useState, type FormEvent } from "react";
import WorkbenchPanels, { TaskQueue } from "../dashboard/Workbench";
import { DESK_LABELS, STREAM_LABELS, parseStaff, parseViews, request, useResource,
  type Stream, type Workbench } from "../dashboard/data";

export function mayReplaceProjection(dirty: boolean, confirmDiscard: () => boolean): boolean {
  return !dirty || confirmDiscard();
}
export function StaffOverview({ data }: { data: Workbench }) {
  return <><div className="dash-kpis"><div><span>سرویس‌های مجاز</span><strong>{data.desks.length.toLocaleString("fa-IR")}</strong></div>
    <div><span>ورودی قابل مشاهده</span><strong>{data.items.length.toLocaleString("fa-IR")}</strong></div>
    <div><span>وظایف تحریریه</span><strong>{data.tasks.length.toLocaleString("fa-IR")}</strong></div></div>
    <p className="dash-note">سرویس‌های واگذارشده: {data.desks.map(d => DESK_LABELS[d]).join("، ") || "هیچ سرویس مجازی ثبت نشده است."}</p>
    <TaskQueue tasks={data.tasks} /></>;
}
function Views({ token, desk, stream, desks, select }: { token: string; desk: string; stream: Stream;
  desks: string[]; select: (d: string, s: Stream) => void }) {
  const q = useResource(token, "/v1/ui/saved-views", parseViews);
  const [name, setName] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  async function change(method: "PUT" | "DELETE", id?: string) {
    if (active.current) return;
    const controller = new AbortController(); active.current = controller; setBusy(true); setMessage("");
    try {
      await request(token, "/v1/ui/saved-views" + (id ? "/" + encodeURIComponent(id) : ""), controller.signal,
        method, method === "PUT" ? { name: name.trim(), desk, stream } : undefined);
      if (!controller.signal.aborted) { setName(""); q.refresh(); }
    } catch { if (!controller.signal.aborted) setMessage("ذخیره یا حذف نما ناموفق بود."); }
    finally { if (active.current === controller) active.current = null; if (!controller.signal.aborted) setBusy(false); }
  }
  return <section className="dash-panel"><h2>نماهای ذخیره‌شده</h2>
    <p className="dash-note">فقط فیلترهای شخصی سرویس‌های مجاز، بدون ذخیره متن خبر یا توکن.</p>
    {q.loading && <p role="status">در حال دریافت نماها…</p>}
    {q.error !== null && <p>مخزن نماهای شخصی در دسترس نیست.</p>}
    {q.data && <>
      <form className="dash-filter" onSubmit={(e: FormEvent) => { e.preventDefault(); void change("PUT"); }}>
        <label>نام نما <input maxLength={64} value={name} onChange={e => setName(e.target.value)} /></label>
        <button disabled={busy || !name.trim() || !desks.includes(desk)} type="submit">ذخیره نمای جاری</button>
      </form>
      {q.data.filter(v => desks.includes(v.filters_json.desk)).map(v => <div className="dash-control" key={v.view_id}>
        <button type="button" onClick={() => select(v.filters_json.desk, v.filters_json.stream)}>{v.name}</button>
        <span>{DESK_LABELS[v.filters_json.desk]} · {STREAM_LABELS[v.filters_json.stream]}</span>
        <button type="button" disabled={busy} onClick={() => void change("DELETE", v.view_id)}>حذف نما</button></div>)}
    </>}
    {message && <p role="alert">{message}</p>}
  </section>;
}
export default function StaffDashboard({ token }: { token: string }) {
  const [desk, setDesk] = useState("");
  const [stream, setStream] = useState<Stream>("all");
  const [draftDirty, setDraftDirty] = useState(false);
  const q = useResource(token, desk ? "/v1/ui/desk/" + encodeURIComponent(desk) + "?stream=" + stream : "/v1/ui/staff-dashboard", parseStaff);
  const confirmDiscard = () => mayReplaceProjection(draftDirty,
    () => window.confirm("تغییرات ذخیره نشده کنار گذاشته شود؟"));
  const select = (d: string, s: Stream) => {
    if (!confirmDiscard()) return;
    setDesk(d); setStream(s);
  };
  return <div className="role-dashboard staff-dashboard">
    <header className="dash-header"><div><p className="eyebrow">میز کار · تحریریه</p><h1>داشبورد کارکنان</h1>
      <p>ورودی، کارهای واگذارشده و وضعیت بررسی در سرویس‌های مجاز</p></div><button type="button"
        onClick={() => { if (confirmDiscard()) q.refresh(); }}>به‌روزرسانی</button></header>
    {q.loading && <p role="status">در حال دریافت میز کار…</p>}
    {q.error !== null && <p role="alert">داده یا مجوز میز کار در دسترس نیست. <button type="button" onClick={() => select("", "all")}>بازگشت به سرویس‌های مجاز</button></p>}
    {q.data && <>
      <div className="dash-filter"><label>سرویس <select value={desk} onChange={e => select(e.target.value, "all")}>
        <option value="">همه سرویس‌های مجاز</option>{q.data.desks.map(d => <option key={d} value={d}>{DESK_LABELS[d]}</option>)}
      </select></label><label>جریان <select disabled={!desk} value={stream}
        onChange={e => select(desk, e.target.value as Stream)}>
        {Object.entries(STREAM_LABELS).map(([s, label]) => <option key={s} value={s}>{label}</option>)}</select></label></div>
      <StaffOverview data={q.data} />
      <Views token={token} desk={desk} stream={stream} desks={q.data.desks} select={select} />
      <WorkbenchPanels token={token} data={q.data} onDraftDirtyChange={setDraftDirty} />
    </>}
  </div>;
}
