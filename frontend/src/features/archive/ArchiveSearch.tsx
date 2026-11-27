import { useEffect, useRef, useState, type FormEvent } from "react";

type Entry = { news_id: string; title: string; stream: string; source_id: string; received_at: string };
type Version = { artifact_id: string; sha256: string; created_at: string };
type Original = { title: string; text: string | null; redacted: boolean };
function object(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}
function strings(row: Record<string, unknown>, keys: string[]) {
  return keys.every(key => typeof row[key] === "string");
}
export function parseArchive(value: unknown): Entry[] {
  if (!object(value) || value.schema_version !== "1.0" || !Array.isArray(value.items) ||
      value.count !== value.items.length || value.items.length > 100 ||
      !value.items.every(row => object(row) && strings(row, ["news_id", "title", "stream", "source_id", "received_at"]) &&
        ["domestic", "foreign", "manual"].includes(String(row.stream)))) throw new Error("archive contract");
  return value.items as Entry[];
}
function parseVersions(value: unknown, newsId: string): Version[] {
  if (!object(value) || value.schema_version !== "1.0" || value.news_id !== newsId ||
      !Array.isArray(value.versions) || value.versions.length > 100 ||
      !value.versions.every(row => object(row) && strings(row, ["artifact_id", "sha256", "created_at"]) &&
        /^raw-[a-f0-9]{64}$/.test(String(row.artifact_id)) && /^[a-f0-9]{64}$/.test(String(row.sha256)))) {
    throw new Error("archive versions");
  }
  return value.versions as Version[];
}
function parseOriginal(value: unknown, newsId: string, id: string): Original {
  if (!object(value) || value.schema_version !== "1.0" || value.artifact_id !== id ||
      typeof value.text_redacted !== "boolean" || !object(value.source) || value.source.news_id !== newsId ||
      typeof value.source.original_title !== "string" ||
      !(value.source.original_text === null || typeof value.source.original_text === "string") ||
      (value.text_redacted && value.source.original_text !== null)) throw new Error("archive source");
  return { title: value.source.original_title, text: value.source.original_text, redacted: value.text_redacted };
}
export default function ArchiveSearch({ token }: { token: string }) {
  const [query, setQuery] = useState("");
  const [stream, setStream] = useState("");
  const [items, setItems] = useState<Entry[]>([]);
  const [selected, setSelected] = useState("");
  const [versions, setVersions] = useState<Version[]>([]);
  const [original, setOriginal] = useState<Original | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  async function load(path: string, apply: (data: unknown) => void) {
    active.current?.abort();
    const controller = new AbortController(); active.current = controller;
    setBusy(true); setMessage("");
    try {
      const response = await fetch(path, { signal: controller.signal, credentials: "same-origin",
        cache: "no-store", redirect: "error", headers: { Authorization: "Bearer " + token, Accept: "application/json" } });
      if (!response.ok) throw new Error(response.status === 401 || response.status === 403 ? "denied" : "unavailable");
      const data: unknown = await response.json();
      if (!controller.signal.aborted) apply(data);
    } catch (error) {
      if (!controller.signal.aborted) setMessage(error instanceof Error && error.message === "denied"
        ? "مجوز دسترسی به این بخش وجود ندارد." : "بازیابی آرشیو ناموفق بود.");
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }
  function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setItems([]); setSelected(""); setVersions([]); setOriginal(null);
    const params = new URLSearchParams({ q: query.trim() });
    if (stream) params.set("stream", stream);
    void load("/v1/archive/news?" + params, data => {
      const entries = parseArchive(data); setItems(entries);
      if (!entries.length) setMessage("در صفحه بررسی‌شده، خبر قابل دسترسی یافت نشد.");
    });
  }
  function choose(entry: Entry) {
    setSelected(entry.news_id); setVersions([]); setOriginal(null);
    void load("/v1/archive/news/" + encodeURIComponent(entry.news_id) + "/versions",
      data => setVersions(parseVersions(data, entry.news_id)));
  }
  function openVersion(row: Version) {
    setOriginal(null);
    void load("/v1/archive/news/" + encodeURIComponent(selected) + "/versions/" + encodeURIComponent(row.artifact_id),
      data => setOriginal(parseOriginal(data, selected, row.artifact_id)));
  }
  return <section className="news-desk" aria-labelledby="archive-heading">
    <h2 id="archive-heading">آرشیو · جست‌وجوی اخبار و نسخه‌های اصلی</h2>
    <form className="grid gap-2" onSubmit={search}>
      <label>عنوان یا نشانی منبع <input maxLength={200} value={query} onChange={event => setQuery(event.target.value)} /></label>
      <label>جریان <select value={stream} onChange={event => setStream(event.target.value)}>
        <option value="">همه</option><option value="domestic">داخلی</option>
        <option value="foreign">خارجی</option><option value="manual">دستی</option>
      </select></label>
      <button disabled={busy} type="submit">جست‌وجو در آرشیو مجاز</button>
    </form>
    <p>نمایش حداکثر ۱۰۰ نامزد اخیر مطابق جست‌وجو و مجوز شما.</p>
    {busy && <p role="status">در حال بازیابی…</p>}{message && <p role="status">{message}</p>}
    <ul className="grid gap-2">{items.map(entry => <li key={entry.news_id}>
      <button type="button" disabled={busy} onClick={() => choose(entry)}>{entry.title}</button>
      <span dir="ltr"> {entry.source_id} </span>
    </li>)}</ul>
    {versions.length > 0 && <div><h3>نسخه‌های اصل خبر</h3>{versions.map(row =>
      <button type="button" disabled={busy} key={row.artifact_id} onClick={() => openVersion(row)}>
        <time dateTime={row.created_at}>{row.created_at}</time> · {row.sha256.slice(0, 12)}
      </button>)}</div>}
    {original && <article className="news-item"><h3 dir="auto">{original.title}</h3>
      {original.redacted ? <p>متن طبق حقوق فعلی منبع قابل نمایش نیست.</p> :
        <p dir="auto" className="whitespace-pre-wrap">{original.text ?? "متن موجود نیست."}</p>}</article>}
  </section>;
}
