import { useEffect, useRef, useState, type FormEvent } from "react";

type EvidenceRef = { source_ref: string; news_id: string; title: string; original_url: string; collected_at: string; stream: string; text_redacted: boolean };
type Claim = { text: string; source_refs: string[]; classification: string };
type Result = { topic?: string; claims?: Claim[]; answer?: string; uncertainties?: string[]; uncertainty_notes?: string[];
  citations?: { source_ref: string; quote_span: string | null }[] };
type Envelope = { result: Result | null; evidence: EvidenceRef[] };
type Report = { title: string; sections: { heading: string; body: string; source_refs: string[] }[] };
type ReportEnvelope = { result: Report | null; evidence: EvidenceRef[] };
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
  const [mode, setMode] = useState<"run" | "chat" | "report">("run");
  const [slot, setSlot] = useState("1");
  const [report, setReport] = useState<ReportEnvelope | null>(null);
  const [result, setResult] = useState<Envelope | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || !query.trim()) return;
    let requestQuery: string;
    try { requestQuery = composeChatQuery(query, mode === "chat" && followup ? context : null); }
    catch { setMessage("مجموع سؤال و موضوع پیشین باید حداکثر ۵۰۰ نویسه باشد."); return; }
    const controller = new AbortController(); active.current = controller;
    setBusy(true); setMessage(""); setResult(null); setReport(null);
    const end = new Date();
    const start = new Date(end.getTime() - Number(days) * 86400000);
    try {
      const response = await fetch("/v1/analysis/" + (mode === "report" ? "reports/" + slot : mode), { method: "POST", signal: controller.signal,
        credentials: "same-origin", cache: "no-store", redirect: "error",
        headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
        body: JSON.stringify({ query: requestQuery, period_start: start.toISOString(),
          period_end: end.toISOString(), stream: stream || null, limit: 12 }) });
      if (response.status === 401 || response.status === 403) throw new Error("denied");
      if (response.status === 422 && mode === "report") throw new Error("disabled");
      if (!response.ok) throw new Error("unavailable");
      const data: unknown = await response.json();
      if (!controller.signal.aborted) {
        if (mode === "report") setReport(parseReport(data));
        else {
          const parsed = parseAnalysis(data, mode);
          setResult(parsed);
          if (mode === "chat" && parsed.evidence.length > 0) {
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
    <h2 id="analysis-heading">ژرفان · تحلیل مستند و گفت‌وگو با آرشیو</h2>
    <p>پاسخ استخراجی از اسناد مجاز؛ مدل خارجی فعال نیست. اختلاف پوشش منابع، اثبات صحت ادعاها نیست.</p>
    <form className="grid gap-2" onSubmit={submit}>
      <fieldset disabled={busy} className="grid gap-2">
        <label>نوع درخواست <select value={mode} onChange={event => {
          setMode(event.target.value === "report" ? "report" : event.target.value === "chat" ? "chat" : "run"); setResult(null); setReport(null); setStream(""); setContext(null); setFollowup(false);
        }}><option value="run">تحلیل روایی و خط زمانی</option><option value="chat">پرسش از آرشیو</option><option value="report">پیش‌نویس گزارش عمومی</option></select></label>
        {mode === "report" && <label>نوع گزارش <select value={slot} onChange={event => {
          setSlot(event.target.value); setReport(null); setStream("");
        }}><option value="1">مرور اخبار ایران</option><option value="2">مرور اخبار جهان</option>
          <option value="3">زاویه‌های پوشش خبری</option><option value="4">جمع‌بندی روز</option></select></label>}
        <label>موضوع یا پرسش <textarea required maxLength={500} value={query} onChange={event => setQuery(event.target.value)} /></label>
        {mode === "chat" && context && <div className="grid gap-1">
          <label><input type="checkbox" checked={followup} onChange={event => setFollowup(event.target.checked)} />
            ادامه موضوع قبلی در جست‌وجو</label>
          <p dir="auto">موضوع قبلی: {context}</p>
          <button type="button" onClick={() => { setContext(null); setFollowup(false); setResult(null); }}>
            پاک کردن زمینه گفت‌وگو
          </button>
          <p>زمینه فقط در این نشست مرورگر باقی می‌ماند؛ منابع دوباره با مجوز جاری بازیابی می‌شوند.</p>
        </div>}
        <label>بازه <select value={days} onChange={event => {
          setDays(event.target.value); setContext(null); setFollowup(false); setResult(null);
        }}>
          <option value="1">۲۴ ساعت اخیر</option><option value="7">۷ روز اخیر</option><option value="30">۳۰ روز اخیر</option></select></label>
        {(mode !== "report" || !["1", "2"].includes(slot)) && <label>جریان <select value={stream} onChange={event => { setStream(event.target.value); setContext(null); setFollowup(false); setResult(null); setReport(null); }}>
          <option value="">همه مجاز</option><option value="domestic">ایران</option><option value="foreign">جهان</option></select></label>}
        <button type="submit" disabled={!query.trim()}>{mode === "report" ? "ساخت پیش‌نویس از آرشیو" : mode === "run" ? "تحلیل اسناد" : "بازیابی پاسخ مستند"}</button>
      </fieldset>
    </form>
    {busy && <p role="status">در حال بازیابی شواهد…</p>}{message && <p role="alert">{message}</p>}
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
