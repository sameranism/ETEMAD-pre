import { useEffect, useRef, useState, type FormEvent } from "react";

type EvidenceRef = { source_ref: string; news_id: string; title: string; original_url: string; collected_at: string; stream: string; text_redacted: boolean };
type Claim = { text: string; source_refs: string[]; classification: string };
type Result = { topic?: string; claims?: Claim[]; answer?: string; uncertainties?: string[]; uncertainty_notes?: string[];
  citations?: { source_ref: string; quote_span: string | null }[] };
type Envelope = { result: Result | null; evidence: EvidenceRef[] };
type Report = { title: string; sections: { heading: string; body: string; source_refs: string[] }[] };
type ReportEnvelope = { result: Report | null; evidence: EvidenceRef[] };
type SavedMemory = { topic: string; days: 1 | 7 | 30; stream: "all" | "domestic" | "foreign" };
type MetricSnapshot = { snapshot_id: string; stream: "mixed" | "domestic" | "foreign" | "manual";
  time_range: { from: string; to: string }; collected_count: number; unique_count: number;
  published_count: number; successful_deliveries: number; failed_deliveries: number;
  unknown_deliveries: number; topic_coverage: { topic: string; count: number }[]; generated_at: string };
type MetricEnvelope = { snapshot: MetricSnapshot | null; evidence: Record<string, unknown> };
const isObject = (x: unknown): x is Record<string, unknown> => typeof x === "object" && x !== null && !Array.isArray(x);
const strings = (x: unknown): x is string[] => Array.isArray(x) && x.every(y => typeof y === "string");
function safeUrl(value: string) {
  try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.href : null; }
  catch { return null; }
}
export function parseAnalysis(value: unknown, mode: "run" | "chat"): Envelope {
  if (!isObject(value) || value.schema_version !== "1.0" || value.mode !== "extractive" ||
      !Array.isArray(value.evidence) || value.evidence.length > 20 ||
      !value.evidence.every(e => isObject(e) && typeof e.source_ref === "string" && /^raw-[a-f0-9]{64}$/.test(e.source_ref) &&
        ["news_id", "title", "original_url", "collected_at"].every(k => typeof e[k] === "string") &&
        ["domestic", "foreign", "manual"].includes(String(e.stream)) && typeof e.text_redacted === "boolean")) {
    throw new Error("Invalid evidence envelope");
  }
  const refs = new Set(value.evidence.map(e => e.source_ref));
  if (refs.size !== value.evidence.length) throw new Error("Duplicate source reference");
  const validRefs = (x: unknown) => strings(x) && x.length > 0 && x.every(ref => refs.has(ref));
  const result = value.result;
  if (mode === "run" && result === null && refs.size === 0) return { result: null, evidence: [] };
  if (!isObject(result) || result.schema_version !== "1.0") throw new Error("Invalid result");
  if (mode === "run") {
    if (typeof result.topic !== "string" || !validRefs(result.source_refs) || !strings(result.uncertainties) ||
        !Array.isArray(result.claims) || !result.claims.every(c => isObject(c) && typeof c.text === "string" &&
          validRefs(c.source_refs) && ["documented", "attributed", "interpretation", "uncertain"].includes(String(c.classification)))) {
      throw new Error("Uncited analysis");
    }
  } else if (result.classification !== "internal" || typeof result.answer !== "string" ||
      !strings(result.uncertainty_notes) || !Array.isArray(result.citations) ||
      !result.citations.every(c => isObject(c) && refs.has(String(c.source_ref)) &&
        (c.quote_span === null || typeof c.quote_span === "string"))) throw new Error("Uncited chat");
  return { result: result as Result, evidence: value.evidence as EvidenceRef[] };
}

export function parseReport(value: unknown): ReportEnvelope {
  if (!isObject(value) || value.schema_version !== "1.0" || value.mode !== "bounded_draft" ||
      value.bounded !== true || !Array.isArray(value.evidence)) throw new Error("Invalid report envelope");
  // Reuse the evidence validator with a synthetic empty extractive result.
  const verified = parseAnalysis({ ...value, mode: "extractive", result: {
    schema_version: "1.0", classification: "internal", answer: "", uncertainty_notes: [], citations: []
  } }, "chat");
  if (value.result === null) return { result: null, evidence: verified.evidence };
  const report = value.result;
  const refs = new Set(verified.evidence.map(e => e.source_ref));
  const validRefs = (x: unknown) => strings(x) && x.length > 0 && x.every(ref => refs.has(ref));
  if (!isObject(report) || report.schema_version !== "1.0" || report.visibility !== "public" ||
      report.generation_status !== "draft" || report.created_by_project !== "P18" ||
      !["domestic_digest", "foreign_digest", "news_angles", "daily_summary"].includes(String(report.report_type)) ||
      typeof report.title !== "string" || !validRefs(report.source_refs) ||
      !Array.isArray(report.sections) || !report.sections.length || !report.sections.every(section =>
        isObject(section) && typeof section.heading === "string" && typeof section.body === "string" &&
        validRefs(section.source_refs))) throw new Error("Invalid public draft");
  return { result: report as Report, evidence: verified.evidence };
}

// Only user-entered context is carried forward, never cached source text or
// evidence. The existing API repeats all current permission checks.
export function composeChatQuery(question: string, context: string | null): string {
  const query = [question.trim(), context?.trim()].filter(Boolean).join("\n");
  if (!question.trim() || Array.from(query).length > 500) throw new Error("context bounds");
  return query;
}

export function parseMemory(value: unknown): SavedMemory | null {
  if (!isObject(value) || value.schema_version !== "1.0") throw new Error("Invalid memory");
  const item = value.memory;
  if (item === null) return null;
  if (!isObject(item) || typeof item.topic !== "string" || !item.topic.trim() ||
      Array.from(item.topic).length > 500 || ![1, 7, 30].includes(Number(item.days)) ||
      typeof item.days !== "number" || !["all", "domestic", "foreign"].includes(String(item.stream)) ||
      typeof item.updated_at !== "string" || !Number.isFinite(Date.parse(item.updated_at))) throw new Error("Invalid memory");
  return { topic: item.topic, days: item.days, stream: item.stream } as SavedMemory;
}

export function parseMetrics(value: unknown): MetricEnvelope {
  if (!isObject(value) || value.schema_version !== "1.0" || value.scope !== "authorized_subject" ||
      !isObject(value.evidence) || !(value.snapshot === null || isObject(value.snapshot))) {
    throw new Error("Invalid metric envelope");
  }
  if (value.snapshot === null) return { snapshot: null, evidence: value.evidence };
  const item = value.snapshot;
  const counts = ["collected_count", "unique_count", "published_count", "successful_deliveries",
    "failed_deliveries", "unknown_deliveries"];
  if (item.schema_version !== "1.0" || typeof item.snapshot_id !== "string" || item.snapshot_id.length === 0 ||
      !["mixed", "domestic", "foreign", "manual"].includes(String(item.stream)) ||
      !isObject(item.time_range) || typeof item.time_range.from !== "string" ||
      typeof item.time_range.to !== "string" || !Number.isFinite(Date.parse(item.time_range.from)) ||
      !Number.isFinite(Date.parse(item.time_range.to)) || typeof item.generated_at !== "string" ||
      !Number.isFinite(Date.parse(item.generated_at)) || counts.some(key =>
        typeof item[key] !== "number" || !Number.isInteger(item[key]) || item[key] < 0) ||
      !Array.isArray(item.topic_coverage) || !item.topic_coverage.every(row => isObject(row) &&
        typeof row.topic === "string" && row.topic.length > 0 && Number.isInteger(row.count) && Number(row.count) >= 0)) {
    throw new Error("Invalid metric snapshot");
  }
  return { snapshot: item as unknown as MetricSnapshot, evidence: value.evidence };
}

export default function PoliticalAnalysis({ token }: { token: string }) {
  // A new authentication session discards the previous conversation state.
  return <AnalysisSession key={token} token={token} />;
}

function AnalysisSession({ token }: { token: string }) {
  const [query, setQuery] = useState("");
  const [context, setContext] = useState<string | null>(null);
  const [followup, setFollowup] = useState(false);
  const [days, setDays] = useState("7");
  const [stream, setStream] = useState("");
  const [mode, setMode] = useState<"auto" | "run" | "chat" | "report">("auto");
  const [selectedTool, setSelectedTool] = useState("");
  const [slot, setSlot] = useState("1");
  const [report, setReport] = useState<ReportEnvelope | null>(null);
  const [metrics, setMetrics] = useState<MetricEnvelope | null>(null);
  const [metricsBusy, setMetricsBusy] = useState(false);
  const [metricsMessage, setMetricsMessage] = useState("");
  const [result, setResult] = useState<Envelope | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const active = useRef<AbortController | null>(null);
  const metricActive = useRef<AbortController | null>(null);
  useEffect(() => () => { active.current?.abort(); metricActive.current?.abort(); }, []);
  async function refreshMetrics() {
    if (busy || metricsBusy) return;
    const controller = new AbortController(); metricActive.current = controller;
    setMetricsBusy(true); setMetricsMessage(""); setMetrics(null);
    const end = new Date();
    const start = new Date(end.getTime() - Number(days) * 86400000);
    try {
      const response = await fetch("/v1/metrics", { method: "POST", signal: controller.signal,
        credentials: "same-origin", cache: "no-store", redirect: "error",
        headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
        body: JSON.stringify({ period_start: start.toISOString(), period_end: end.toISOString(),
          stream: stream || "mixed" }) });
      if (response.status === 401 || response.status === 403) throw new Error("denied");
      if (!response.ok) throw new Error("unavailable");
      const parsed = parseMetrics(await response.json());
      if (!controller.signal.aborted) {
        setMetrics(parsed);
        if (parsed.snapshot === null) setMetricsMessage("داده کامل و قابل اتکای این بازه هنوز در دسترس نیست.");
      }
    } catch (error) {
      if (!controller.signal.aborted) setMetricsMessage(error instanceof Error && error.message === "denied"
        ? "مجوز مشاهده شاخص‌های تحریریه وجود ندارد." : "محاسبه شاخص‌ها در دسترس نیست.");
    } finally { if (!controller.signal.aborted) setMetricsBusy(false); }
  }
  async function memoryAction(method: "GET" | "PUT" | "DELETE") {
    if (busy || (method === "PUT" && !context)) return;
    const controller = new AbortController(); active.current = controller;
    setBusy(true); setMessage("");
    try {
      const response = await fetch("/v1/analysis/chat/memory", {
        method, signal: controller.signal, credentials: "same-origin", cache: "no-store", redirect: "error",
        headers: { Authorization: "Bearer " + token, ...(method === "PUT" ? { "Content-Type": "application/json" } : {}) },
        ...(method === "PUT" ? { body: JSON.stringify({ topic: context, days: Number(days), stream: stream || "all" }) } : {})
      });
      if (!response.ok) throw new Error("memory unavailable");
      const memory = parseMemory(await response.json());
      if (controller.signal.aborted) return;
      if (method === "GET" && memory) {
        setContext(memory.topic); setDays(String(memory.days)); setStream(memory.stream === "all" ? "" : memory.stream);
        setFollowup(true); setResult(null); setMessage("موضوع ذخیره‌شده بازیابی شد.");
      } else if (method === "GET") setMessage("موضوع ذخیره‌شده‌ای وجود ندارد.");
      else if (method === "PUT" && memory) setMessage("موضوع برای نشست‌های بعدی ذخیره شد.");
      else if (method === "DELETE" && memory === null) {
        setContext(null); setFollowup(false); setResult(null); setMessage("حافظه ذخیره‌شده حذف شد.");
      } else throw new Error("memory response mismatch");
    } catch {
      if (!controller.signal.aborted) setMessage("حافظه پایدار در دسترس نیست یا مجوز کافی وجود ندارد.");
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || !query.trim()) return;
    let requestQuery: string;
    try { requestQuery = composeChatQuery(query, (mode === "chat" || mode === "auto") && followup ? context : null); }
    catch { setMessage("مجموع سؤال و موضوع پیشین باید حداکثر ۵۰۰ نویسه باشد."); return; }
    const controller = new AbortController(); active.current = controller;
    setBusy(true); setMessage(""); setResult(null); setReport(null); setSelectedTool("");
    const end = new Date();
    const start = new Date(end.getTime() - Number(days) * 86400000);
    try {
      const response = await fetch("/v1/analysis/assistant", { method: "POST", signal: controller.signal,
        credentials: "same-origin", cache: "no-store", redirect: "error",
        headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
        body: JSON.stringify({ action: mode === "auto" ? "auto" : mode === "chat" ? "research" : mode === "run" ? "analysis" : "public_report",
          ...(mode === "report" ? { report_slot: Number(slot) } : {}),
          evidence: { query: requestQuery, period_start: start.toISOString(),
            period_end: end.toISOString(), stream: stream || null, limit: 12 } }) });
      if (response.status === 401 || response.status === 403) throw new Error("denied");
      if (response.status === 422 && mode === "report") throw new Error("disabled");
      if (!response.ok) throw new Error("unavailable");
      const data: unknown = await response.json();
      if (!controller.signal.aborted) {
        if (!isObject(data)) throw new Error("invalid assistant response");
        const action = data.assistant_action;
        if (mode === "report" && action === "public_report") {
          setReport(parseReport(data)); setSelectedTool("تولید گزارش");
        } else {
          const resolved = action === "research" ? "chat" : action === "analysis" ? "run" : null;
          if (!resolved || (mode !== "auto" && mode !== resolved)) throw new Error("invalid assistant action");
          const parsed = parseAnalysis(data, resolved);
          setResult(parsed); setSelectedTool(resolved === "chat" ? "پژوهش خبری" : "تحلیل رویدادها");
          if (resolved === "chat" && parsed.evidence.length > 0) {
            if (!followup || !context) setContext(query.trim());
            setQuery("");
          }
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) setMessage(error instanceof Error && error.message === "denied"
        ? "مجوز تحلیل یا دسترسی به شواهد آرشیو وجود ندارد." :
        error instanceof Error && error.message === "disabled" ? "گزارش عمومی در تنظیمات خروجی غیرفعال است." : "بازیابی شواهد ناموفق بود.");
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }
  return <section className="news-desk" aria-labelledby="analysis-heading">
    <h2 id="analysis-heading">دستیار هوشمند اعتماد <span lang="en" dir="ltr">ETEMAD AI Assistant</span></h2>
    <p>دستیار اختصاصی تحریریه اعتماد؛ پژوهش خبری، تحلیل رویدادها و تهیه پیش‌نویس گزارش با استفاده از ابزارهای مجاز سامانه.</p>
    <p>استودیوی عمومی اشتراکی محصول مستقلی است و به این فضای سازمانی دسترسی ندارد.</p>
    <p>نسخه فعلی: انتخاب محلی ابزار بر اساس درخواست و پاسخ استخراجی از اسناد مجاز؛ مدل مولد خارجی فعال نیست. اختلاف پوشش منابع، اثبات صحت ادعاها نیست.</p>
    <section className="news-item" aria-labelledby="metrics-heading">
      <h3 id="metrics-heading">شاخص‌های مستند تحریریه</h3>
      <p>اعداد فقط از snapshot کاملِ آرشیو، پردازش و نتیجه انتشار در بازه انتخابی ساخته می‌شوند؛ نبود مخرج به‌صورت «ناموجود» نمایش داده می‌شود.</p>
      <button type="button" disabled={busy || metricsBusy} onClick={() => void refreshMetrics()}>
        {metricsBusy ? "در حال محاسبه…" : "به‌روزرسانی شاخص‌های بازه"}
      </button>
      {metricsMessage && <p role="alert">{metricsMessage}</p>}
      {metrics?.snapshot && <div className="grid gap-2" role="status">
        <p>دریافت‌شده: {metrics.snapshot.collected_count} · یکتا: {metrics.snapshot.unique_count} · منتشرشده: {metrics.snapshot.published_count}</p>
        <p>تحویل موفق: {metrics.snapshot.successful_deliveries} · ناموفق: {metrics.snapshot.failed_deliveries} · نامشخص: {metrics.snapshot.unknown_deliveries}</p>
        <p>مخرج تحویل: {metrics.snapshot.successful_deliveries + metrics.snapshot.failed_deliveries + metrics.snapshot.unknown_deliveries || "ناموجود"}</p>
        {metrics.snapshot.topic_coverage.length > 0 && <ul>{metrics.snapshot.topic_coverage.map(row =>
          <li key={row.topic}>{row.topic}: {row.count}</li>)}</ul>}
      </div>}
    </section>
    <form className="grid gap-2" onSubmit={submit}>
      <fieldset disabled={busy} className="grid gap-2">
        <label>ابزار دستیار <select value={mode} onChange={event => {
          setMode(event.target.value === "report" ? "report" : event.target.value === "chat" ? "chat" : event.target.value === "run" ? "run" : "auto"); setResult(null); setReport(null); setStream(""); setContext(null); setFollowup(false); setSelectedTool("");
        }}><option value="auto">انتخاب خودکار پژوهش یا تحلیل</option><option value="run">تحلیل روایی و خط زمانی</option><option value="chat">پژوهش خبری و پرسش از آرشیو</option><option value="report">پیش‌نویس گزارش عمومی</option></select></label>
        {mode === "report" && <label>نوع گزارش <select value={slot} onChange={event => {
          setSlot(event.target.value); setReport(null); setStream("");
        }}><option value="1">مرور اخبار ایران</option><option value="2">مرور اخبار جهان</option>
          <option value="3">زاویه‌های پوشش خبری</option><option value="4">جمع‌بندی روز</option></select></label>}
        <label>موضوع یا پرسش <textarea required maxLength={500} value={query} onChange={event => setQuery(event.target.value)} /></label>
        {(mode === "chat" || mode === "auto") && context && <div className="grid gap-1">
          <label><input type="checkbox" checked={followup} onChange={event => setFollowup(event.target.checked)} />
            ادامه موضوع قبلی در جست‌وجو</label>
          <p dir="auto">موضوع قبلی: {context}</p>
          <button type="button" onClick={() => { setContext(null); setFollowup(false); setResult(null); }}>
            پاک کردن زمینه گفت‌وگو
          </button>
          <p>منابع دوباره با مجوز جاری بازیابی می‌شوند؛ پاسخ‌ها و اسناد در حافظه ذخیره نمی‌شوند.</p>
          <button type="button" onClick={() => void memoryAction("PUT")}>ذخیره موضوع برای نشست بعدی</button>
        </div>}
        {(mode === "chat" || mode === "auto") && <div className="grid gap-1">
          <button type="button" onClick={() => void memoryAction("GET")}>بازیابی موضوع ذخیره‌شده</button>
          <button type="button" onClick={() => void memoryAction("DELETE")}>حذف حافظه پایدار من</button>
          <p>ذخیره اختیاری است؛ فقط موضوعی که خودتان وارد کرده‌اید و بازه و جریان انتخاب‌شده ذخیره می‌شود.</p>
        </div>}
        <label>بازه <select value={days} onChange={event => {
          setDays(event.target.value); setContext(null); setFollowup(false); setResult(null);
        }}>
          <option value="1">۲۴ ساعت اخیر</option><option value="7">۷ روز اخیر</option><option value="30">۳۰ روز اخیر</option></select></label>
        {(mode !== "report" || !["1", "2"].includes(slot)) && <label>جریان <select value={stream} onChange={event => { setStream(event.target.value); setContext(null); setFollowup(false); setResult(null); setReport(null); }}>
          <option value="">همه مجاز</option><option value="domestic">ایران</option><option value="foreign">جهان</option></select></label>}
        <button type="submit" disabled={!query.trim()}>{mode === "report" ? "ساخت پیش‌نویس از آرشیو" : mode === "run" ? "تحلیل اسناد" : mode === "auto" ? "ارسال به دستیار" : "بازیابی پاسخ مستند"}</button>
      </fieldset>
    </form>
    {busy && <p role="status">در حال بازیابی شواهد…</p>}{message && <p role="alert">{message}</p>}
    {selectedTool && (result || report) && <p role="status">ابزار استفاده‌شده: {selectedTool}</p>}
    {report && <div className="grid gap-3">
      <p>پیش‌نویس محدود به موضوع و اسناد مجاز است؛ گزارش کامل روزانه یا تأیید انتشار نیست.</p>
      {!report.result && <p>شاهد مرتبط یافت نشد.</p>}
      {report.result && <article className="news-item"><h3>{report.result.title}</h3>
        {report.result.sections.map((section, index) => <section key={index}><h4>{section.heading}</h4>
          <p dir="auto" className="whitespace-pre-wrap">{section.body}</p>
          {section.source_refs.map(ref => <a key={ref} href={"#report-" + ref}>شاهد {report.evidence.findIndex(e => e.source_ref === ref) + 1} </a>)}
        </section>)}</article>}
      {report.evidence.map((e, i) => <article className="news-item" key={e.source_ref} id={"report-" + e.source_ref}>
        <h3>شاهد {i+1} · {e.title}</h3>
        {safeUrl(e.original_url) && <a href={safeUrl(e.original_url)!} target="_blank" rel="noopener noreferrer">منبع اصلی</a>}
      </article>)}
    </div>}
    {result && <div className="grid gap-3">
      {!result.result && <p>شاهد مرتبط در نمونه مجاز یافت نشد؛ نتیجه قطعی در دسترس نیست.</p>}
      {result.result?.answer && <p dir="auto" className="whitespace-pre-wrap">{result.result.answer}</p>}
      {result.result?.claims?.map((claim, index) => <article key={index} className="news-item">
        <p dir="auto" className="whitespace-pre-wrap">{claim.text}</p>
        <p>{claim.classification === "documented" ? "مشاهده مجموعه اسناد" : "منتسب به منبع"}</p>
        {claim.source_refs.map(ref => <a key={ref} href={"#evidence-" + ref}>شاهد {result.evidence.findIndex(e => e.source_ref === ref) + 1} </a>)}
      </article>)}
      {[...(result.result?.uncertainties ?? []), ...(result.result?.uncertainty_notes ?? [])].map((note, i) => <p key={i}>{note}</p>)}
      {result.evidence.map((e, i) => <article className="news-item" key={e.source_ref} id={"evidence-" + e.source_ref}>
        <h3>شاهد {i+1} · {e.title}</h3><time dateTime={e.collected_at}>{e.collected_at}</time>
        {safeUrl(e.original_url) && <p><a href={safeUrl(e.original_url)!} target="_blank" rel="noopener noreferrer">منبع اصلی</a></p>}
        {e.text_redacted && <p>فقط فراداده؛ متن به دلیل حقوق استفاده نمایش داده نمی‌شود.</p>}
      </article>)}
    </div>}
  </section>;
}
