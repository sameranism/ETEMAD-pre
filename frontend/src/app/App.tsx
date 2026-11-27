import { useEffect, useState, type FormEvent } from "react";
import Login from "../features/auth/Login";
import { authMode, type AuthMode } from "../lib/auth";
import DashboardSession from "../features/dashboard/DashboardSession";
export { parseNewsFeed } from "../lib/news";

export default function App() {
  const [token, setToken] = useState("");
  const [draft, setDraft] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [mode, setMode] = useState<AuthMode | "loading" | "error">("loading");
  const [expiry, setExpiry] = useState<number | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    authMode(controller.signal).then(value => {
      if (!controller.signal.aborted) setMode(value);
    }).catch(() => { if (!controller.signal.aborted) setMode("error"); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (expiry === null || !token) return;
    const timer = window.setTimeout(disconnect, Math.max(0, expiry - Date.now()));
    return () => window.clearTimeout(timer);
  }, [expiry, token]);
  function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = draft.trim();
    if (!next || mode !== "fixture") return;
    setToken(next); setExpiry(null); setDraft(""); setAttempt(n => n + 1);
  }
  function staffSession(next: string, expiresIn: number) {
    setExpiry(Date.now() + expiresIn * 1000); setToken(next); setAttempt(n => n + 1);
  }
  function disconnect() { setToken(""); setExpiry(null); setDraft(""); }
  return <div className="app-frame" dir="rtl">
    <header className="masthead">
      <div><p className="eyebrow">رابط تحریریه</p><h1>اعتماد <span>· میز خبر</span></h1></div>
      {token && <button type="button" onClick={disconnect}>قطع نشست</button>}
    </header>
    <main>
      {!token ? (
        mode === "staff" ? <Login onSession={staffSession} /> :
        mode === "loading" ? <p role="status">در حال بررسی وضعیت ورود…</p> :
        mode === "error" ? <p role="alert">سرویس ورود در دسترس نیست. صفحه را دوباره بارگذاری کنید.</p> :
        <form onSubmit={connect}>
          <h2>اتصال به نشست مجاز تحریریه</h2>
          <label htmlFor="session-token">توکن نشست صادرشده توسط سامانه</label>
          <input id="session-token" type="password" dir="ltr" autoComplete="off"
            value={draft} onChange={event => setDraft(event.target.value)} required />
          <button type="submit" disabled={!draft.trim()}>اتصال</button>
          <p>توکن فقط در حافظه این صفحه نگهداری می‌شود.</p>
        </form>
      ) : <DashboardSession key={attempt} token={token} onUnauthorized={disconnect} />}
    </main>
  </div>;
}
