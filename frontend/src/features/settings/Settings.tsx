import { useEffect, useRef, useState, type FormEvent } from "react";

const CONFIG_ID = "SCH-DOMESTIC-INGEST";
const SCOPE = "domestic";
const PATH = "/v1/config/schedule_config/" + CONFIG_ID;
type Schedule = { revision: number; enabled: boolean; minutes: number };
type State = "loading" | "ready" | "missing" | "forbidden" | "error" | "conflict";
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseDomesticSchedule(value: unknown): Schedule {
  if (!object(value) || value.schema_version !== "1.0" ||
      value.contract_name !== "schedule_config" || value.config_id !== CONFIG_ID ||
      value.scope !== SCOPE || typeof value.revision !== "number" ||
      !Number.isSafeInteger(value.revision) || value.revision < 1 ||
      !object(value.value)) throw new Error("Invalid schedule envelope");
  const config = value.value;
  if (config.schema_version !== "1.0" || config.config_id !== CONFIG_ID ||
      config.stream !== "domestic" || config.desk !== "all" || config.mode !== "interval" ||
      typeof config.timezone !== "string" ||
      !Array.isArray(config.fixed_times_local) || config.fixed_times_local.length !== 0 ||
      typeof config.interval_minutes !== "number" || !Number.isInteger(config.interval_minutes) ||
      config.interval_minutes < 1 || config.interval_minutes > 1440 ||
      typeof config.enabled !== "boolean" || typeof config.updated_at !== "string" ||
      !Number.isFinite(Date.parse(config.updated_at))) throw new Error("Invalid domestic schedule");
  return { revision: value.revision, enabled: config.enabled, minutes: config.interval_minutes };
}

export function scheduleError(status: number): "forbidden" | "conflict" | "error" {
  if (status === 401 || status === 403) return "forbidden";
  if (status === 409) return "conflict";
  return "error";
}

function headers(token: string) {
  return { Authorization: "Bearer " + token, Accept: "application/json" };
}

export default function Settings({ token }: { token: string }) {
  const [state, setState] = useState<State>("loading");
  const [revision, setRevision] = useState(0);
  const [enabled, setEnabled] = useState(false);
  const [minutes, setMinutes] = useState("5");
  const [attempt, setAttempt] = useState(0);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setState("loading");
    fetch(PATH + "?scope=" + SCOPE, {
      signal: controller.signal, headers: headers(token), credentials: "same-origin",
      cache: "no-store", redirect: "error",
    }).then(async response => {
      if (controller.signal.aborted) return;
      if (response.status === 404) {
        setRevision(0); setEnabled(false); setMinutes("5"); setState("missing"); return;
      }
      if (!response.ok) { setState(scheduleError(response.status)); return; }
      const result = parseDomesticSchedule(await response.json());
      if (controller.signal.aborted) return;
      setRevision(result.revision); setEnabled(result.enabled);
      setMinutes(String(result.minutes)); setState("ready");
    }).catch(() => { if (!controller.signal.aborted) setState("error"); });
    return () => { controller.abort(); pending.current?.abort(); };
  }, [token, attempt]);

  const interval = Number(minutes);
  const valid = minutes.trim() !== "" && Number.isInteger(interval) && interval >= 1 && interval <= 1440;
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending.current || !valid || (state !== "ready" && state !== "missing")) return;
    const controller = new AbortController();
    pending.current = controller;
    setSaving(true); setSaved(false);
    try {
      const response = await fetch(PATH, {
        method: "PUT", signal: controller.signal, credentials: "same-origin", cache: "no-store",
        redirect: "error", headers: { ...headers(token), "Content-Type": "application/json" },
        body: JSON.stringify({
          scope: SCOPE, expected_revision: revision,
          value: { schema_version: "1.0", config_id: CONFIG_ID, stream: "domestic",
            desk: "all", timezone: "Asia/Tehran", mode: "interval", fixed_times_local: [],
            interval_minutes: interval, enabled, updated_at: new Date().toISOString() },
        }),
      });
      if (controller.signal.aborted) return;
      if (!response.ok) { setState(scheduleError(response.status)); return; }
      const value: unknown = await response.json();
      if (!object(value) || value.schema_version !== "1.0" || value.config_id !== CONFIG_ID ||
          value.scope !== SCOPE || value.contract_name !== "schedule_config" ||
          value.revision !== revision + 1) throw new Error("Invalid update response");
      if (controller.signal.aborted) return;
      setRevision(revision + 1); setSaved(true); setState("loading");
      setAttempt(n => n + 1);
    } catch {
      if (!controller.signal.aborted) setState("error");
    } finally {
      if (pending.current === controller) pending.current = null;
      if (!controller.signal.aborted) setSaving(false);
    }
  }

  return <section className="news-desk" aria-labelledby="schedule-heading">
    <div className="section-heading"><div><p className="eyebrow">مرکز تنظیمات</p>
      <h2 id="schedule-heading">زمان‌بندی ایران‌بان</h2></div></div>
    {state === "loading" && <p role="status" className="message">در حال دریافت تنظیمات…</p>}
    {state === "forbidden" && <p role="alert" className="message">مجوز مشاهده یا تغییر تنظیمات را ندارید.</p>}
    {state === "conflict" && <p role="alert" className="message">تنظیمات تغییر کرده است. نسخه تازه را دریافت کنید؛ تغییر قبلی بازنویسی نمی‌شود.</p>}
    {state === "error" && <p role="alert" className="message">دریافت یا ذخیره تنظیمات ناموفق بود. پیش از تلاش مجدد نسخه فعلی را دریافت کنید.</p>}
    {(state === "conflict" || state === "error") && <button type="button" onClick={() => {
      setSaved(false); setAttempt(n => n + 1);
    }}>دریافت نسخه تازه</button>}
    {(state === "ready" || state === "missing") && <form className="news-item" onSubmit={save}>
      <p>{state === "missing" ? "هنوز برنامه‌ای ثبت نشده است؛ دریافت دوره‌ای خاموش است." :
        "نسخه تنظیمات: " + revision.toLocaleString("fa-IR")}</p>
      <p>این تنظیم فقط برای دریافت داخلی است؛ به انتشار یا منابع خارجی مربوط نیست.</p>
      <div className="flex flex-wrap gap-4 items-center my-4">
        <label htmlFor="domestic-interval">فاصله دریافت (دقیقه)</label>
        <input id="domestic-interval" type="number" min={1} max={1440} step={1} required
          className="border rounded p-2 w-32" dir="ltr" value={minutes} disabled={saving}
          onChange={event => { setMinutes(event.target.value); setSaved(false); }} />
        <label className="flex gap-2 items-center">
          <input type="checkbox" checked={enabled} disabled={saving}
            onChange={event => { setEnabled(event.target.checked); setSaved(false); }} />
          دریافت دوره‌ای فعال باشد
        </label>
      </div>
      <button className="border rounded px-4 py-2" type="submit" disabled={!valid || saving}>
        {saving ? "در حال ذخیره…" : "ذخیره تنظیمات"}
      </button>
      {saved && <p role="status">تنظیمات ذخیره شد و نسخه ثبت‌شده دریافت شد.</p>}
    </form>}
  </section>;
}
