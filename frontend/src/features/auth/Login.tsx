import { useEffect, useRef, useState, type FormEvent } from "react";
import { login, resetFirstLogin } from "../../lib/auth";

type Props = { onSession: (token: string, expiresIn: number) => void };

export default function Login({ onSession }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [replacement, setReplacement] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [mustReset, setMustReset] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending.current) return;
    if (mustReset && (replacement.length < 12 || replacement !== confirmation || replacement === password)) {
      setError("گذرواژه جدید باید حداقل ۱۲ نویسه، متفاوت از گذرواژه اولیه و با تکرار آن یکسان باشد.");
      return;
    }
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setError("");
    try {
      const result = mustReset
        ? await resetFirstLogin(username.trim(), password, replacement, controller.signal)
        : await login(username.trim(), password, controller.signal);
      if (controller.signal.aborted) return;
      if (result.mustReset) {
        setMustReset(true);
        return;
      }
      setPassword("");
      setReplacement("");
      setConfirmation("");
      onSession(result.token, result.expiresIn);
    } catch (reason) {
      if (!controller.signal.aborted) {
        setError(reason instanceof Error ? reason.message : "ورود ناموفق بود.");
      }
    } finally {
      if (!controller.signal.aborted) {
        pending.current = null;
        setBusy(false);
      }
    }
  }

  return (
    <form onSubmit={submit} dir="rtl" aria-busy={busy}>
      <h2>{mustReset ? "تغییر گذرواژه اولیه" : "ورود به تحریریه"}</h2>
      <p>{mustReset ? "پیش از دسترسی، گذرواژه شخصی خود را تعیین کنید." : "با حساب کاربری اختصاصی وارد شوید."}</p>
      <label htmlFor="login-username">نام کاربری</label>
      <input id="login-username" value={username} onChange={(event) => setUsername(event.target.value)}
        autoComplete="username" required maxLength={64} disabled={busy || mustReset} />
      <label htmlFor="login-password">{mustReset ? "گذرواژه اولیه" : "گذرواژه"}</label>
      <input id="login-password" type="password" value={password}
        onChange={(event) => setPassword(event.target.value)}
        autoComplete="current-password" required maxLength={256} disabled={busy || mustReset} />
      {mustReset && <>
        <label htmlFor="new-password">گذرواژه جدید</label>
        <input id="new-password" type="password" value={replacement} autoComplete="new-password"
          onChange={(event) => setReplacement(event.target.value)} minLength={12} maxLength={256} required disabled={busy} />
        <label htmlFor="confirm-password">تکرار گذرواژه جدید</label>
        <input id="confirm-password" type="password" value={confirmation} autoComplete="new-password"
          onChange={(event) => setConfirmation(event.target.value)} minLength={12} maxLength={256} required disabled={busy} />
      </>}
      {error && <p role="alert">{error}</p>}
      <button type="submit" disabled={busy || !username.trim() || !password}>
        {busy ? "در حال بررسی…" : mustReset ? "ثبت گذرواژه و ورود" : "ورود"}
      </button>
      {mustReset && <button type="button" disabled={busy} onClick={() => {
        setMustReset(false);
        setPassword("");
        setReplacement("");
        setConfirmation("");
        setError("");
      }}>بازگشت</button>}
    </form>
  );
}
