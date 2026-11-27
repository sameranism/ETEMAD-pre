import { useEffect, useRef, useState } from "react";

type Slot = { slot: number; title: string; private: boolean; generation_enabled: boolean };
type Saved = { payload: { title: string; report_id: string; generated_at: string;
  visibility: string; sections: { heading: string; body: string; source_refs: string[] }[] };
  evidence: { source_ref: string; title: string }[] };
const object = (x: unknown): x is Record<string, unknown> => !!x && typeof x === "object" && !Array.isArray(x);

function parseSlots(x: unknown): Slot[] {
  if (!object(x) || x.schema_version !== "1.0" || !Array.isArray(x.slots) || x.slots.length > 7 ||
      !x.slots.every(s => object(s) && typeof s.slot === "number" && Number.isInteger(s.slot) &&
        s.slot >= 1 && s.slot <= 7 && typeof s.title === "string" && s.private === (s.slot >= 5) &&
        typeof s.generation_enabled === "boolean")) throw new Error("invalid slots");
  return x.slots as Slot[];
}
function parseSaved(x: unknown, slot: Slot): Saved | null {
  if (!object(x) || x.schema_version !== "1.0" || x.slot !== slot.slot || x.mode !== "bounded_daily_draft" ||
      x.bounded !== true) throw new Error("invalid report");
  const saved = x.saved;
  if (saved === null) return null;
  if (!object(saved) || !object(saved.payload) || !Array.isArray(saved.evidence) ||
      saved.evidence.length > 20 || !saved.evidence.every(e => object(e) &&
        typeof e.source_ref === "string" && /^raw-[a-f0-9]{64}$/.test(e.source_ref) &&
        typeof e.title === "string")) throw new Error("invalid evidence");
  const p = saved.payload;
  const refs = new Set(saved.evidence.map(e => e.source_ref));
  if (p.visibility !== (slot.private ? "executive_private" : "public") ||
      (slot.private && p.publishable !== false) || p.generation_status !== "draft" ||
      typeof p.title !== "string" || typeof p.report_id !== "string" || typeof p.generated_at !== "string" ||
      !Array.isArray(p.sections) || !p.sections.every(s => object(s) && typeof s.heading === "string" &&
        typeof s.body === "string" && Array.isArray(s.source_refs) &&
        s.source_refs.every(ref => typeof ref === "string" && refs.has(ref)))) throw new Error("invalid payload");
  return saved as Saved;
}

export default function DailyReports({ token }: { token: string }) {
  const [slots, setSlots] = useState<Slot[]>([]);
  const [results, setResults] = useState<Record<number, Saved | null>>({});
  const [messages, setMessages] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const active = useRef<AbortController | null>(null);
  const path = "/v1/analysis/daily-reports";
  useEffect(() => {
    const controller = new AbortController(); active.current = controller;
    setLoaded(false); setSlots([]); setResults({}); setMessages({});
    fetch(path, { signal: controller.signal, credentials: "same-origin", cache: "no-store",
      redirect: "error", headers: { Authorization: "Bearer " + token } })
      .then(async r => { if (!r.ok) return []; return parseSlots(await r.json()); })
      .then(rows => { if (!controller.signal.aborted) setSlots(rows); })
      .catch(() => { if (!controller.signal.aborted) setSlots([]); })
      .finally(() => { if (!controller.signal.aborted) setLoaded(true); });
    return () => active.current?.abort();
  }, [token]);
  async function run(selected: Slot[], method: "GET" | "POST") {
    if (busy) return;
    const controller = new AbortController(); active.current = controller;
    setBusy(true);
    const end = new Date();
    const start = new Date(end.getTime() - 86400000);
    try {
      for (const slot of selected) {
        if (controller.signal.aborted) break;
        try {
          const r = await fetch(path + "/" + slot.slot, { method, signal: controller.signal,
            credentials: "same-origin", cache: "no-store", redirect: "error",
            headers: { Authorization: "Bearer " + token, ...(method === "POST" ? { "Content-Type": "application/json" } : {}) },
            ...(method === "POST" ? { body: JSON.stringify({ period_start: start.toISOString(),
              period_end: end.toISOString() }) } : {}) });
          if (!r.ok) throw new Error("unavailable");
          const result = parseSaved(await r.json(), slot);
          if (!controller.signal.aborted) {
            setResults(old => ({ ...old, [slot.slot]: result }));
            setMessages(old => ({ ...old, [slot.slot]: result ? "" : "گزارش ذخیره‌شده یا شاهد کافی وجود ندارد." }));
          }
        } catch {
          if (!controller.signal.aborted) {
            setResults(old => ({ ...old, [slot.slot]: null }));
            setMessages(old => ({ ...old, [slot.slot]: "گزارش یا مجوز دسترسی در دسترس نیست." }));
          }
        }
      }
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }
  if (!loaded || !slots.length) return null;
  return <section className="news-desk" aria-label="مرکز گزارش‌های مدیریتی">
    <h2>گزارش‌های مدیریتی</h2>
    <p>پیش‌نویس روزانه از حداکثر ۲۰ سند مجاز؛ بدون ادعای پوشش کامل یا مجوز انتشار. گزارش محرمانه فقط برای کاربر مجاز است.</p>
    <button type="button" disabled={busy || !slots.some(s => s.generation_enabled)}
      onClick={() => void run(slots.filter(s => s.generation_enabled), "POST")}>ساخت همه گزارش‌های مجاز ۲۴ ساعت اخیر</button>
    <button type="button" disabled={busy} onClick={() => void run(slots, "GET")}>بازیابی آخرین گزارش‌های ذخیره‌شده</button>
    {busy && <p role="status">در حال پردازش گزارش‌ها…</p>}
    {slots.map(slot => <article key={slot.slot} className="news-item grid gap-2">
      <h3>{slot.title} {slot.private && " · محرمانه"}</h3>
      <button type="button" disabled={busy || !slot.generation_enabled}
        onClick={() => void run([slot], "POST")}>ساخت و ذخیره گزارش</button>
      {!slot.generation_enabled && <p>تولید گزارش عمومی در کنترل‌های خروجی غیرفعال است.</p>}
      <button type="button" disabled={busy} onClick={() => void run([slot], "GET")}>آخرین نسخه ذخیره‌شده</button>
      {messages[slot.slot] && <p role="status">{messages[slot.slot]}</p>}
      {results[slot.slot] && <div>
        <p>زمان تولید: {new Date(results[slot.slot]!.payload.generated_at).toLocaleString("fa-IR")}</p>
        {results[slot.slot]!.payload.sections.map((s, i) => <section key={i}>
          <h4>{s.heading}</h4><p dir="auto">{s.body}</p>
          <p>شواهد: {s.source_refs.map(ref => results[slot.slot]!.evidence.find(e => e.source_ref === ref)?.title).filter(Boolean).join("، ")}</p>
        </section>)}
      </div>}
    </article>)}
  </section>;
}
