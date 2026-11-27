import { useEffect, useRef, useState } from "react";

type Job = { job_id: string; object_id: string; object_type: string; stream: string;
  destination_id: string; state: string; scheduled_at: string; expires_at: string;
  can_hold: boolean; attempt: { status: string; message_id: string | null; error_code: string | null } | null };
type Queue = { publishing_enabled: boolean; bounded: boolean; items: Job[] };
const states: Record<string, string> = { scheduled: "زمان‌بندی شده", held: "متوقف", in_progress: "در حال ارسال",
  completed: "پایان‌یافته", cancelled: "لغوشده", success: "موفق ثبت‌شده", failed: "ناموفق",
  unknown: "نتیجه نامشخص", skipped: "ارسال نشده" };
const obj = (x: unknown): x is Record<string, unknown> => !!x && typeof x === "object" && !Array.isArray(x);
const strings = (x: Record<string, unknown>, keys: string[]) => keys.every(k => typeof x[k] === "string");
function parseQueue(x: unknown): Queue {
  if (!obj(x) || x.schema_version !== "1.0" || x.mode !== "fixture_history" ||
      x.live_dispatch_available !== false || typeof x.publishing_enabled !== "boolean" ||
      typeof x.bounded !== "boolean" || !Array.isArray(x.items) || x.items.length > 100 ||
      !x.items.every(j => obj(j) && strings(j, ["job_id", "object_id", "object_type", "stream", "destination_id",
        "state", "scheduled_at", "expires_at"]) && ["article", "public_report"].includes(String(j.object_type)) &&
        ["scheduled", "held", "in_progress", "completed", "cancelled"].includes(String(j.state)) &&
        typeof j.can_hold === "boolean" && (j.attempt === null || (obj(j.attempt) &&
          ["success", "failed", "unknown", "skipped"].includes(String(j.attempt.status)) &&
          (j.attempt.message_id === null || typeof j.attempt.message_id === "string") &&
          (j.attempt.error_code === null || typeof j.attempt.error_code === "string"))))) {
    throw new Error("invalid publication queue");
  }
  return x as Queue;
}
export default function PublishingCenter({ token }: { token: string }) {
  const [queue, setQueue] = useState<Queue | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const active = useRef<AbortController | null>(null);
  const path = "/v1/publishing/queue";
  async function load(controller: AbortController) {
    const response = await fetch(path, { signal: controller.signal, credentials: "same-origin", cache: "no-store",
      redirect: "error", headers: { Authorization: "Bearer " + token } });
    if (!response.ok) throw new Error("unavailable");
    const result = parseQueue(await response.json());
    if (!controller.signal.aborted) setQueue(result);
  }
  useEffect(() => {
    const controller = new AbortController(); active.current = controller;
    setQueue(null); setBusy(true); setNotice("");
    load(controller).catch(() => { if (!controller.signal.aborted) setQueue(null); })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => active.current?.abort();
  // Component is keyed to the authenticated session.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);
  async function refresh(job?: Job) {
    if (busy) return;
    const controller = new AbortController(); active.current = controller;
    setBusy(true); setNotice("");
    try {
      if (job) {
        const response = await fetch(path + "/" + encodeURIComponent(job.job_id) + "/hold", {
          method: "POST", signal: controller.signal, credentials: "same-origin", cache: "no-store", redirect: "error",
          headers: { Authorization: "Bearer " + token }
        });
        if (!response.ok) throw new Error("hold unavailable");
        if (!controller.signal.aborted) setNotice("کار ارسال‌نشده متوقف شد.");
      }
      await load(controller);
    } catch { if (!controller.signal.aborted) setNotice("عملیات در دسترس نیست یا وضعیت کار تغییر کرده است."); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  if (!queue) return null;
  return <section className="news-desk" aria-label="مرکز انتشار">
    <h2>رسان · مرکز انتشار</h2>
    <p>وضعیت واقعی صف ثبت‌شده در پایگاه داده. سوابق فعلی آزمایشی‌اند؛ ارسال خارجی هنوز متصل نیست.</p>
    <p>کلید انتشار: {queue.publishing_enabled ? "روشن" : "خاموش"}</p>
    <button type="button" disabled={busy} onClick={() => void refresh()}>به‌روزرسانی صف</button>
    {notice && <p role="status">{notice}</p>}
    {!queue.items.length && <p>کاری در صف ثبت نشده است.</p>}
    {queue.bounded && <p>۱۰۰ کار آخر نمایش داده می‌شود.</p>}
    {queue.items.map(j => <article key={j.job_id} className="news-item grid gap-1">
      <h3>{j.object_type === "article" ? "خبر" : "گزارش عمومی"} · {states[j.state]}</h3>
      <p>مقصد: <bdi>{j.destination_id}</bdi> · جریان: {j.stream}</p>
      <p>زمان انتشار: {new Date(j.scheduled_at).toLocaleString("fa-IR")}</p>
      {j.attempt && <p>آخرین تلاش: {states[j.attempt.status]}
        {j.attempt.error_code && <> · <bdi>{j.attempt.error_code}</bdi></>}
        {j.attempt.message_id && <> · شناسه: <bdi>{j.attempt.message_id}</bdi></>}
      </p>}
      {j.can_hold && <button type="button" disabled={busy} onClick={() => void refresh(j)}>توقف کار ارسال‌نشده</button>}
    </article>)}
  </section>;
}
