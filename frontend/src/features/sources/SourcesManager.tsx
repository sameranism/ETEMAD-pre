import { useState } from "react";

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
