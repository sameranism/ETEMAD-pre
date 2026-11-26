import { useEffect, useState } from "react";

export type SourceRecord = {
  source_id: string;
  site_url: string;
  feed_url?: string | null;
  country: string;
  language: string;
  stream: "domestic" | "foreign";
  license_scope: "metadata" | "summary" | "full_text" | "unknown";
  enabled: boolean;
};

type LoadState = "ready" | "loading" | "error" | "forbidden";

type Props = {
  sources: SourceRecord[];
  state?: LoadState;
};

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isHttpUrl(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const parsed = new URL(value);
    return (parsed.protocol === "http:" || parsed.protocol === "https:") && Boolean(parsed.hostname);
  } catch {
    return false;
  }
}

function isSource(value: unknown): value is SourceRecord {
  if (!isObject(value)) return false;
  return typeof value.source_id === "string" && value.source_id.length > 0 &&
    isHttpUrl(value.site_url) &&
    (value.feed_url === undefined || value.feed_url === null || isHttpUrl(value.feed_url)) &&
    typeof value.country === "string" && /^[A-Z]{2}$/.test(value.country) &&
    typeof value.language === "string" && value.language.length > 0 &&
    (value.stream === "domestic" || value.stream === "foreign") &&
    (value.license_scope === "metadata" || value.license_scope === "summary" ||
      value.license_scope === "full_text" || value.license_scope === "unknown") &&
    typeof value.enabled === "boolean";
}

export function parseSources(value: unknown): SourceRecord[] {
  if (!isObject(value) || value.schema_version !== "1.0" ||
      !Array.isArray(value.sources) || !value.sources.every(isSource)) {
    throw new Error("Invalid source registry response");
  }
  const sources: SourceRecord[] = value.sources;
  if (new Set(sources.map((source) => source.source_id)).size !== sources.length) {
    throw new Error("Duplicate source identity");
  }
  return [...sources].sort((a, b) => a.source_id.localeCompare(b.source_id));
}

export default function SourcesManager({ sources, state = "ready" }: Props) {
  const [stream, setStream] = useState<"all" | "domestic" | "foreign">("all");
  const visible = sources.filter((source) => stream === "all" || source.stream === stream);

  return (
    <section aria-labelledby="sources-heading" className="news-desk">
      <div className="section-heading">
        <div>
          <p className="eyebrow">دفتر منابع</p>
          <h2 id="sources-heading">منابع مجاز و نامزدها</h2>
        </div>
        <span className="count" aria-live="polite">
          {visible.length.toLocaleString("fa-IR")} منبع
        </span>
      </div>
      <div className="filters" role="group" aria-label="فیلتر جریان منابع">
        {(["all", "domestic", "foreign"] as const).map((value) => (
          <button key={value} type="button" aria-pressed={stream === value}
            onClick={() => setStream(value)}>
            {value === "all" ? "همه" : value === "domestic" ? "داخلی" : "خارجی"}
          </button>
        ))}
      </div>
      {state === "loading" && <p role="status" className="message">در حال دریافت منابع…</p>}
      {state === "error" && <p role="alert" className="message">دریافت دفتر منابع ناموفق بود.</p>}
      {state === "forbidden" && <p role="alert" className="message">دسترسی به دفتر منابع مجاز نیست.</p>}
      {state === "ready" && visible.length === 0 &&
        <p className="message">منبعی در این جریان ثبت نشده است.</p>}
      {state === "ready" && visible.length > 0 && (
        <ul className="news-list">
          {visible.map((source) => (
            <li className="news-item" key={source.source_id}>
              <div className="news-meta">
                <span>{source.stream === "domestic" ? "داخلی" : "خارجی"}</span>
                <span dir="ltr">{source.source_id}</span>
                <span dir="ltr">{source.country} · {source.language}</span>
              </div>
              <h3 dir="auto">{new URL(source.site_url).hostname}</h3>
              <p>
                {source.enabled
                  ? source.license_scope === "unknown"
                    ? "فعال‌سازی مسدود: حق استفاده نامشخص"
                    : `فعال · دامنه حق استفاده: ${source.license_scope}`
                  : source.stream === "foreign"
                    ? "نامزد خارجی غیرفعال؛ تا تصویب قابل پایش نیست"
                    : "منبع غیرفعال"}
              </p>
              <a href={source.site_url} target="_blank" rel="noopener noreferrer">
                وب‌سایت منبع <span className="sr-only">(در پنجره جدید)</span>
              </a>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

type ForeignCandidate = {
  source_id: string;
  country: string;
  language: string;
  verified: boolean;
  enabled: false;
};
type ForeignPoll = {
  source_id: string;
  revision: number;
  last_success_at: string | null;
  next_poll_at: string;
  consecutive_failures: number;
};
export type ForeignStatus = {
  candidate_count: number;
  verified_candidate_count: number;
  raw_count: number;
  delivered_count: number;
  received_count: number;
  processed_count: number;
  candidates: ForeignCandidate[];
  polls: ForeignPoll[];
};
const count = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const dateTime = (value: unknown): value is string =>
  typeof value === "string" && Number.isFinite(Date.parse(value));

export function parseForeignStatus(value: unknown): ForeignStatus {
  if (!isObject(value) || value.schema_version !== "1.0" || value.mode !== "fixture" ||
      !count(value.candidate_count) || !count(value.verified_candidate_count) ||
      !count(value.raw_count) || !count(value.delivered_count) ||
      !count(value.received_count) || !count(value.processed_count) ||
      value.verified_candidate_count > value.candidate_count ||
      value.delivered_count > value.raw_count || value.processed_count > value.received_count ||
      !Array.isArray(value.candidates) || value.candidates.length > 200 ||
      !Array.isArray(value.polls) || value.polls.length > 200) {
    throw new Error("Invalid foreign monitor response");
  }
  const candidates: ForeignCandidate[] = value.candidates.map((row: unknown) => {
    if (!isObject(row) || typeof row.source_id !== "string" || !row.source_id ||
        typeof row.country !== "string" || !/^[A-Z]{2}$/.test(row.country) ||
        typeof row.language !== "string" || !row.language ||
        typeof row.verified !== "boolean" || row.enabled !== false) {
      throw new Error("Invalid foreign candidate");
    }
    return { source_id: row.source_id, country: row.country, language: row.language,
      verified: row.verified, enabled: false };
  });
  const polls: ForeignPoll[] = value.polls.map((row: unknown) => {
    if (!isObject(row) || typeof row.source_id !== "string" || !row.source_id ||
        !count(row.revision) || row.revision < 1 || !count(row.consecutive_failures) ||
        (row.last_success_at !== null && !dateTime(row.last_success_at)) ||
        !dateTime(row.next_poll_at)) {
      throw new Error("Invalid foreign poll status");
    }
    return { source_id: row.source_id, revision: row.revision,
      last_success_at: row.last_success_at, next_poll_at: row.next_poll_at,
      consecutive_failures: row.consecutive_failures };
  });
  if (new Set(candidates.map(row => row.source_id)).size !== candidates.length ||
      new Set(polls.map(row => row.source_id)).size !== polls.length) {
    throw new Error("Duplicate foreign status identity");
  }
  return { candidate_count: value.candidate_count,
    verified_candidate_count: value.verified_candidate_count, raw_count: value.raw_count,
    delivered_count: value.delivered_count, received_count: value.received_count,
    processed_count: value.processed_count, candidates, polls };
}

export function ForeignMonitor({ token }: { token: string }) {
  const [status, setStatus] = useState<ForeignStatus | null>(null);
  const [state, setState] = useState<LoadState | "unavailable">("loading");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setStatus(null);
    setState("loading");
    fetch("/v1/foreign/status?limit=50", {
      signal: controller.signal, credentials: "same-origin", cache: "no-store",
      redirect: "error", headers: { Authorization: "Bearer " + token, Accept: "application/json" },
    }).then(async response => {
      if (controller.signal.aborted) return;
      if (response.status === 401 || response.status === 403) {
        setState("forbidden"); return;
      }
      if (response.status === 404) { setState("unavailable"); return; }
      if (!response.ok) throw new Error("Foreign monitor unavailable");
      const result = parseForeignStatus(await response.json());
      if (!controller.signal.aborted) { setStatus(result); setState("ready"); }
    }).catch(() => {
      if (!controller.signal.aborted) { setStatus(null); setState("error"); }
    });
    return () => controller.abort();
  }, [token, attempt]);

  if (state === "forbidden" || state === "unavailable") return null;

  return <section className="news-desk" aria-labelledby="foreign-monitor-heading">
    <div className="section-heading"><div>
      <p className="eyebrow">رصد خارجی · محیط آزمایشی خصوصی</p>
      <h2 id="foreign-monitor-heading">جهان‌بان</h2>
    </div><button type="button" disabled={state === "loading"} onClick={() => setAttempt(n => n + 1)}>
      به‌روزرسانی
    </button></div>
    {state === "loading" && <p role="status" className="message">در حال دریافت وضعیت جهان‌بان…</p>}
    {state === "error" && <p role="alert" className="message">دریافت وضعیت جهان‌بان ناموفق بود.</p>}
    {state === "ready" && status && <>
      <div className="grid grid-cols-2 lg:grid-cols-3 gap-3 my-5">
        {([
          ["نامزدهای کشف‌شده", status.candidate_count],
          ["فیدهای بررسی‌شده", status.verified_candidate_count],
          ["خبر خام خارجی", status.raw_count],
          ["رسید انتقال", status.delivered_count],
          ["دریافت در هسته", status.received_count],
          ["خبر پردازش‌شده", status.processed_count],
        ] as const).map(([label, value]) => <div key={label} className="news-item">
          <p>{label}</p><strong className="text-2xl">{value.toLocaleString("fa-IR")}</strong>
        </div>)}
      </div>
      <p className="message">داده‌های آزمایشی از پایگاه‌های داده واقعی سامانه خوانده شده‌اند؛ دریافت زنده خارجی فعال نیست.</p>
      <h3 className="my-3">نامزدهای منابع خارجی</h3>
      {status.candidates.length === 0 && <p className="message">نامزدی ثبت نشده است.</p>}
      <ul className="news-list">{status.candidates.map(row => <li key={row.source_id} className="news-item">
        <div className="news-meta"><span dir="ltr">{row.source_id}</span>
          <span dir="ltr">{row.country} · {row.language}</span></div>
        <p>{row.verified ? "فید از نظر فنی بررسی شده" : "فید بررسی‌نشده"} · نامزد غیرفعال</p>
      </li>)}</ul>
      <h3 className="my-3">وضعیت دریافت</h3>
      {status.polls.length === 0 && <p className="message">دریافتی ثبت نشده است.</p>}
      <ul className="news-list">{status.polls.map(row => <li key={row.source_id} className="news-item">
        <div className="news-meta"><span dir="ltr">{row.source_id}</span>
          <span>خطاهای متوالی: {row.consecutive_failures.toLocaleString("fa-IR")}</span></div>
        <p>آخرین دریافت موفق: {row.last_success_at ?
          new Date(row.last_success_at).toLocaleString("fa-IR", { timeZone: "Asia/Tehran" }) : "ثبت نشده"}</p>
      </li>)}</ul>
    </>}
  </section>;
}
