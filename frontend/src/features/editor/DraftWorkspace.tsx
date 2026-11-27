import { useEffect, useRef, useState, type FormEvent } from "react";
import TranslatorWorkspace from "../translation/TranslatorWorkspace";
import type { NewsFeedRecord } from "../newsroom/NewsDesk";

type Fields = { title: string; lead: string; body: string; short_summary: string };
type Saved = {
  version: number; source_hash: string; source_snapshot: { original_title: string; original_text: string };
  article: Fields & { desk: string; status: "draft" | "held" };
  translation: { translated_text: string } | null; quality: { passed: boolean; reason_codes: string[] };
  policy: { decision: string; reason_codes: string[] };
};
type Workspace = {
  source: { news_id: string; original_title: string; original_text: string; language: string; stream: string };
  source_hash: string; desks: string[]; latest_version: number; saved: Saved | null;
  history: { version: number; source_hash: string; created_at: string }[];
};
const blank: Fields = { title: "", lead: "", body: "", short_summary: "" };
const keys = ["title", "lead", "body", "short_summary"] as const;
const labels = { title: "تیتر", lead: "لید", body: "متن خبر", short_summary: "خلاصه کوتاه" };
function object(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}
function strings(x: unknown): x is string[] {
  return Array.isArray(x) && x.every(y => typeof y === "string");
}
function version(x: unknown): x is number { return typeof x === "number" && Number.isSafeInteger(x) && x >= 0; }
function hash(x: unknown): x is string { return typeof x === "string" && /^[a-f0-9]{64}$/.test(x); }

export function parseWorkspace(value: unknown, newsId: string): Workspace {
  if (!object(value) || value.schema_version !== "1.0" || value.mode !== "manual" ||
      !object(value.source) || value.source.news_id !== newsId || !hash(value.source_hash) ||
      !["original_title", "original_text", "language", "stream"].every(k => typeof (value.source as Record<string, unknown>)[k] === "string") ||
      !strings(value.desks) || !value.desks.length || !version(value.latest_version) ||
      !Array.isArray(value.history) || !value.history.every(row => object(row) && version(row.version) &&
        row.version > 0 && hash(row.source_hash) && typeof row.created_at === "string")) {
    throw new Error("Invalid workspace");
  }
  const saved = value.saved;
  if (saved !== null && (!object(saved) || !version(saved.version) || saved.version < 1 ||
      saved.version > value.latest_version || !hash(saved.source_hash) ||
      !object(saved.source_snapshot) || saved.source_snapshot.news_id !== newsId ||
      typeof saved.source_snapshot.original_title !== "string" ||
      typeof saved.source_snapshot.original_text !== "string" ||
      !object(saved.article) || saved.article.news_id !== newsId ||
      !keys.every(k => typeof (saved.article as Record<string, unknown>)[k] === "string") ||
      typeof saved.article.desk !== "string" || !["draft", "held"].includes(String(saved.article.status)) ||
      (saved.translation !== null && (!object(saved.translation) || saved.translation.news_id !== newsId ||
        typeof saved.translation.translated_text !== "string")) ||
      !object(saved.quality) || typeof saved.quality.passed !== "boolean" || !strings(saved.quality.reason_codes) ||
      !object(saved.policy) || !["blocked", "human_review"].includes(String(saved.policy.decision)) ||
      !strings(saved.policy.reason_codes))) throw new Error("Invalid saved revision");
  return value as unknown as Workspace;
}

export default function DraftWorkspace({ token, items }: { token: string; items: readonly NewsFeedRecord[] }) {
  const [selected, setSelected] = useState("");
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [fields, setFields] = useState<Fields>(blank);
  const [desk, setDesk] = useState("");
  const [translation, setTranslation] = useState("");
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [message, setMessage] = useState("");
  const [reload, setReload] = useState(0);
  const [viewVersion, setViewVersion] = useState<number | null>(null);
  const request = useRef<AbortController | null>(null);
  // Retain the same key for the same submission after a lost response.
  const retry = useRef<{ payload: string; key: string } | null>(null);
  const headers = { Authorization: "Bearer " + token, Accept: "application/json" };
  const path = "/v1/editorial/news/" + encodeURIComponent(selected) + "/draft";

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    request.current = controller;
    setWorkspace(null); setBusy(true); setMessage("");
    const url = path + (viewVersion === null ? "" : "?version=" + viewVersion);
    fetch(url, { signal: controller.signal, credentials: "same-origin", cache: "no-store",
      redirect: "error", headers: { Authorization: "Bearer " + token, Accept: "application/json" } })
      .then(async response => {
        if (response.status === 401 || response.status === 403) throw new Error("denied");
        if (!response.ok) throw new Error("unavailable");
        const data = parseWorkspace(await response.json(), selected);
        if (controller.signal.aborted) return;
        setWorkspace(data);
        setFields(data.saved ? Object.fromEntries(keys.map(k => [k, data.saved!.article[k]])) as Fields :
          { ...blank, title: data.source.original_title });
        setDesk(data.saved?.article.desk ?? data.desks[0]);
        setTranslation(data.saved?.translation?.translated_text ?? "");
        setDirty(false);
        retry.current = null;
      }).catch(error => {
        if (!controller.signal.aborted) setMessage(error instanceof Error && error.message === "denied"
          ? "مجوز دسترسی به این خبر یا پیش‌نویس وجود ندارد." : "بازیابی پیش‌نویس ناموفق بود.");
      }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [selected, token, path, reload, viewVersion]);

  useEffect(() => () => request.current?.abort(), []);

  function changeField(key: keyof Fields, value: string) {
    setFields(previous => ({ ...previous, [key]: value })); setDirty(true);
  }
  function changeSelection(value: string) {
    if (busy || (dirty && !window.confirm("تغییرات ذخیره نشده کنار گذاشته شود؟"))) return;
    request.current?.abort(); setSelected(value); setViewVersion(null); setWorkspace(null);
    setFields(blank); setDirty(false); setMessage(""); retry.current = null;
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!workspace || busy || viewVersion !== null) return;
    const payload = JSON.stringify({ ...fields, desk, expected_version: workspace.latest_version,
      source_hash: workspace.source_hash, translated_text: translation.trim() ? translation : null });
    if (retry.current?.payload !== payload) retry.current = { payload, key: crypto.randomUUID() };
    const controller = new AbortController(); request.current = controller;
    setBusy(true); setMessage("");
    try {
      const response = await fetch(path, { method: "PUT", signal: controller.signal,
        credentials: "same-origin", cache: "no-store", redirect: "error",
        headers: { ...headers, "Content-Type": "application/json", "Idempotency-Key": retry.current.key },
        body: payload });
      if (controller.signal.aborted) return;
      if (response.status === 409) {
        setMessage("نسخه یا منبع تغییر کرده است. متن فعلی حفظ شد؛ پیش از ادامه نسخه جدید را بارگیری کنید.");
        return;
      }
      if (!response.ok) throw new Error("save failed");
      setDirty(false); retry.current = null; setReload(v => v + 1);
    } catch {
      if (!controller.signal.aborted) setMessage("ذخیره تأیید نشد. متن حفظ شده؛ دوباره تلاش کنید.");
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }
  const staleSource = workspace?.saved && workspace.saved.source_hash !== workspace.source_hash;
  const displayedSource = viewVersion !== null ? workspace?.saved?.source_snapshot : workspace?.source;
  return (
    <section className="news-desk" aria-labelledby="draft-workspace-heading">
      <h2 id="draft-workspace-heading">نگار · پیش‌نویس و تاریخچه خبر</h2>
      <label className="grid gap-2">خبر ذخیره‌شده
        <select value={selected} disabled={busy} onChange={e => changeSelection(e.target.value)}>
          <option value="">انتخاب خبر</option>
          {items.filter(item => item.content_scope !== "metadata" && !["blocked", "link_only"].includes(item.reuse_permission))
            .map(item => <option key={item.news_id} value={item.news_id}>{item.title}</option>)}
        </select>
      </label>
      {busy && <p role="status">در حال پردازش…</p>}
      {message && <p role="alert">{message}</p>}
      {selected && !busy && <button type="button" onClick={() => {
        if (!dirty || window.confirm("تغییرات ذخیره نشده کنار گذاشته شود؟")) {
          setViewVersion(null); setReload(v => v + 1);
        }
      }}>بارگیری آخرین نسخه</button>}
      {workspace && (
        <div className="grid gap-4 md:grid-cols-2">
          <div className="news-item">
            <h3>اصل خبر (فقط خواندنی)</h3>
            <p dir="auto">{displayedSource?.original_title}</p>
            <p className="whitespace-pre-wrap" dir="auto">{displayedSource?.original_text}</p>
            <p>آخرین نسخه: {workspace.latest_version.toLocaleString("fa-IR")}</p>
            {workspace.history.map(row => <button key={row.version} type="button" disabled={busy}
              onClick={() => { if (!dirty || window.confirm("تغییرات ذخیره نشده کنار گذاشته شود؟")) setViewVersion(row.version); }}>
              نسخه {row.version.toLocaleString("fa-IR")}
            </button>)}
          </div>
          <form className="news-item grid gap-3" onSubmit={save}>
            {viewVersion !== null && <p>نمایش نسخه تاریخی (فقط خواندنی). برای ویرایش، آخرین نسخه را بارگیری کنید.</p>}
            {staleSource && <p role="alert">اصل منبع نسبت به نسخه ذخیره شده تغییر کرده است؛ پیش از ذخیره، متن را با اصل فعلی تطبیق دهید.</p>}
            <fieldset disabled={busy || viewVersion !== null} className="grid gap-3">
              <label>سرویس خبر
                <select value={desk} disabled={workspace.latest_version > 0}
                  onChange={e => { setDesk(e.target.value); setDirty(true); }}>
                  {workspace.desks.map(d => <option key={d}>{d}</option>)}
                </select>
              </label>
              {workspace.source.stream === "foreign" && workspace.source.language !== "fa" &&
                <TranslatorWorkspace sourceLanguage={workspace.source.language} value={translation}
                  disabled={busy || viewVersion !== null} onChange={value => { setTranslation(value); setDirty(true); }} />}
              {keys.map(key => <label key={key} className="grid gap-2">{labels[key]}
                <textarea className="border rounded p-2" value={fields[key]} maxLength={60000} required
                  onChange={e => changeField(key, e.target.value)} /></label>)}
              <button type="submit" disabled={!dirty && workspace.latest_version > 0}>ذخیره نسخه جدید</button>
            </fieldset>
            {workspace.saved && <div>
              <p>نسخه ذخیره‌شده: {workspace.saved.version} · {workspace.saved.article.status === "held" ? "متوقف" : "پیش‌نویس"}</p>
              <p>کنترل قطعی: {workspace.saved.quality.reason_codes.join("، ")}</p>
              {workspace.saved.translation && <p>ترجمه دستی ذخیره شده است؛ نیازمند بررسی معنایی.</p>}
              <p>مجوز انتشار صادر نشده است: {workspace.saved.policy.reason_codes.join("، ")}</p>
            </div>}
          </form>
        </div>
      )}
    </section>
  );
}
