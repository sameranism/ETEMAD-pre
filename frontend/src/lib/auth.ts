export type AuthMode = "fixture" | "staff";
export type AuthResult =
  | { mustReset: true }
  | { mustReset: false; mfaRequired: true; challenge: string; expiresIn: number }
  | { mustReset: false; token: string; expiresIn: number };

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export async function authMode(signal: AbortSignal): Promise<AuthMode> {
  const response = await fetch("/v1/auth/status", {
    signal, cache: "no-store", credentials: "same-origin", redirect: "error",
  });
  const value: unknown = await response.json();
  if (!response.ok || !object(value) || value.schema_version !== "1.0" ||
      (value.mode !== "fixture" && value.mode !== "staff")) {
    throw new Error("وضعیت ورود در دسترس نیست.");
  }
  return value.mode;
}

export function parseAuthResult(value: unknown): AuthResult {
  if (!object(value) || value.schema_version !== "1.0") throw new Error("پاسخ ورود نامعتبر است.");
  if (value.must_reset === true && !("access_token" in value)) return { mustReset: true };
  if (value.must_reset === false && value.mfa_required === true && !("access_token" in value) &&
      typeof value.challenge === "string" && /^mc\.[A-Za-z0-9_-]{43}$/.test(value.challenge) &&
      typeof value.expires_in === "number" && Number.isInteger(value.expires_in) &&
      value.expires_in > 0 && value.expires_in <= 120) {
    return { mustReset: false, mfaRequired: true, challenge: value.challenge, expiresIn: value.expires_in };
  }
  if (value.must_reset === false && value.mfa_required !== true && value.token_type === "bearer" &&
      typeof value.access_token === "string" && value.access_token.length > 0 &&
      typeof value.expires_in === "number" && Number.isInteger(value.expires_in) &&
      value.expires_in > 0 && value.expires_in <= 900) {
    return { mustReset: false, token: value.access_token, expiresIn: value.expires_in };
  }
  throw new Error("پاسخ ورود نامعتبر است.");
}

async function submit(path: string, payload: Record<string, string>, signal: AbortSignal): Promise<AuthResult> {
  const response = await fetch(path, {
    method: "POST", signal, cache: "no-store", credentials: "same-origin",
    redirect: "error", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (response.status === 429) throw new Error("تعداد تلاش‌ها زیاد است. یک دقیقه دیگر تلاش کنید.");
  if (response.status === 401) throw new Error("ورود مجاز نیست یا اطلاعات ورود نادرست است.");
  if (response.status === 400) throw new Error("گذرواژه جدید باید معتبر و غیرتکراری باشد.");
  if (!response.ok) throw new Error("سرویس ورود در دسترس نیست.");
  return parseAuthResult(await response.json());
}

export function login(username: string, password: string, signal: AbortSignal) {
  return submit("/v1/auth/login", { username, password }, signal);
}

export function resetFirstLogin(username: string, temporaryPassword: string, newPassword: string, signal: AbortSignal) {
  return submit("/v1/auth/first-login/reset", {
    username, temporary_password: temporaryPassword, new_password: newPassword,
  }, signal);
}

export function verifyMfa(challenge: string, code: string, signal: AbortSignal) {
  return submit("/v1/auth/mfa/verify", { challenge, code }, signal);
}

export async function revokePrivilegedSession(token: string): Promise<void> {
  const response = await fetch("/v1/auth/privileged/logout", {
    method: "POST", cache: "no-store", credentials: "same-origin", redirect: "error",
    headers: { Authorization: "Bearer " + token },
  });
  if (!response.ok && response.status !== 401) throw new Error("ابطال نشست تأیید نشد.");
}
