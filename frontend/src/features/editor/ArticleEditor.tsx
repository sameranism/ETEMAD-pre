import { useRef, useState, type FormEvent } from "react";

type Replacement = {
  rule_id: string;
  original: string;
  proposed: string;
  action: "applied" | "review" | "blocked";
  reason: string;
};

export type EditorialPreview = {
  rules_version: string;
  original: string;
  proposed: string;
  replacements: Replacement[];
  quality: {
    passed: boolean;
    reason_codes: string[];
    evidence_refs: string[];
    source_hash: string;
    draft_hash: string;
  };
};

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseEditorialPreview(value: unknown): EditorialPreview {
  if (!object(value) || value.schema_version !== "1.0" ||
      typeof value.rules_version !== "string" || !value.rules_version ||
      typeof value.original !== "string" || typeof value.proposed !== "string" ||
      !Array.isArray(value.replacements) || !object(value.quality)) {
    throw new Error("Invalid editorial preview");
  }
  const replacements = value.replacements.map((item: unknown) => {
    if (!object(item) || typeof item.rule_id !== "string" ||
        typeof item.original !== "string" || typeof item.proposed !== "string" ||
        !["applied", "review", "blocked"].includes(String(item.action)) ||
        typeof item.reason !== "string") throw new Error("Invalid replacement");
    return item as Replacement;
  });
  const quality = value.quality;
  if (typeof quality.passed !== "boolean" ||
      !Array.isArray(quality.reason_codes) ||
      !quality.reason_codes.every((item: unknown) => typeof item === "string") ||
      !Array.isArray(quality.evidence_refs) ||
      !quality.evidence_refs.every((item: unknown) => typeof item === "string") ||
      typeof quality.source_hash !== "string" || typeof quality.draft_hash !== "string") {
    throw new Error("Invalid quality result");
  }
  return {
    rules_version: value.rules_version,
    original: value.original,
    proposed: value.proposed,
    replacements,
    quality: quality as EditorialPreview["quality"],
  };
}

type State = "idle" | "loading" | "ready" | "forbidden" | "error";

export default function ArticleEditor({ token }: { token: string }) {
  const [sourceRef, setSourceRef] = useState("");
  const [source, setSource] = useState("");
  const [draft, setDraft] = useState("");
  const [fromText, setFromText] = useState("");
  const [toText, setToText] = useState("");
  const [requiredName, setRequiredName] = useState("");
  const [attributed, setAttributed] = useState(true);
  const [state, setState] = useState<State>("idle");
  const [result, setResult] = useState<EditorialPreview | null>(null);
  const pending = useRef<AbortController | null>(null);

  async function preview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending.current || !sourceRef.trim() || !source.trim() || !draft.trim() ||
        !fromText.trim() || !toText.trim()) return;
    const controller = new AbortController();
    pending.current = controller;
    setState("loading");
    setResult(null);
    const refs = [sourceRef.trim()];
    try {
      const response = await fetch("/v1/editorial/rules/preview", {
        method: "POST",
        signal: controller.signal,
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
        headers: {
          Authorization: "Bearer " + token,
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          ruleset: {
            schema_version: "1.0",
            ruleset_id: "RULE-UI-PREVIEW",
            revision: 1,
            stream: "domestic",
            desk: "all",
            replacement_rules: [{
              rule_id: "RULE-UI-1",
              from_text: fromText.trim(),
              to_text: toText.trim(),
              mode: "auto_replace",
              applies_to: ["body"],
              exempt_quotes: true,
              requires_fact_check: false,
            }],
            prohibited_content_categories: [],
            updated_at: new Date().toISOString(),
          },
          source_text: source,
          draft_text: draft,
          stream: "domestic",
          desk: "all",
          field: "body",
          source_refs: refs,
          attributed_refs: attributed ? refs : [],
          required_names: requiredName.trim() ? [requiredName.trim()] : [],
          claim_evidence: {},
        }),
      });
      if (controller.signal.aborted) return;
      if (response.status === 401 || response.status === 403) {
        setState("forbidden");
        return;
      }
      if (!response.ok) throw new Error("Editorial preview unavailable");
      setResult(parseEditorialPreview(await response.json()));
      setState("ready");
    } catch {
      if (!controller.signal.aborted) setState("error");
    } finally {
      if (pending.current === controller) pending.current = null;
    }
  }

  return (
    <section className="news-desk" aria-labelledby="editor-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">نگار · پیش‌نمایش بدون انتشار</p>
          <h2 id="editor-heading">کنترل واژه‌نامه و کیفیت</h2>
        </div>
      </div>
      <form className="news-item grid gap-4" onSubmit={preview}>
        <p>متن منبع تغییر نمی‌کند؛ نتیجه فقط پیش‌نمایش و شاهد کنترل کیفیت است.</p>
        <label className="grid gap-2">شناسه منبع
          <input className="border rounded p-2" dir="ltr" value={sourceRef}
            onChange={event => setSourceRef(event.target.value)} maxLength={128} required />
        </label>
        <label className="grid gap-2">متن اصلی منبع
          <textarea className="border rounded p-2 min-h-28" value={source}
            onChange={event => setSource(event.target.value)} maxLength={100000} required />
        </label>
        <label className="grid gap-2">پیش‌نویس تحریریه
          <textarea className="border rounded p-2 min-h-28" value={draft}
            onChange={event => setDraft(event.target.value)} maxLength={100000} required />
        </label>
        <div className="grid gap-4 md:grid-cols-2">
          <label className="grid gap-2">عبارت فعلی
            <input className="border rounded p-2" value={fromText}
              onChange={event => setFromText(event.target.value)} maxLength={512} required />
          </label>
          <label className="grid gap-2">عبارت پیشنهادی
            <input className="border rounded p-2" value={toText}
              onChange={event => setToText(event.target.value)} maxLength={512} required />
          </label>
        </div>
        <label className="grid gap-2">نامی که باید حفظ شود
          <input className="border rounded p-2" value={requiredName}
            onChange={event => setRequiredName(event.target.value)} maxLength={256} />
        </label>
        <label className="flex gap-2 items-center">
          <input type="checkbox" checked={attributed}
            onChange={event => setAttributed(event.target.checked)} />
          انتساب منبع در پیش‌نویس ثبت شده است
        </label>
        <button className="border rounded px-4 py-2" type="submit" disabled={state === "loading"}>
          {state === "loading" ? "در حال بررسی…" : "اجرای پیش‌نمایش"}
        </button>
      </form>
      {state === "forbidden" &&
        <p className="message" role="alert">مجوز استفاده از فضای نگار را ندارید.</p>}
      {state === "error" &&
        <p className="message" role="alert">پیش‌نمایش ناموفق بود؛ هیچ تغییری ذخیره نشد.</p>}
      {state === "ready" && result && (
        <div className="news-item" aria-live="polite">
          <p>نسخه قواعد: <span dir="ltr">{result.rules_version}</span></p>
          <h3>متن پیشنهادی</h3>
          <p dir="auto">{result.proposed}</p>
          <p>{result.quality.passed
            ? "کنترل‌های قطعی این پیش‌نمایش عبور کرد."
            : "نیازمند بررسی: " + result.quality.reason_codes.join("، ")}</p>
          <p>{result.replacements.length.toLocaleString("fa-IR")} تغییر ثبت شد.</p>
        </div>
      )}
    </section>
  );
}
