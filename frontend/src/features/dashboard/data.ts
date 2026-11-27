import { useEffect, useState } from "react";
import { parseNewsFeed } from "../../lib/news";
import type { NewsFeedRecord } from "../newsroom/NewsDesk";

export const DESK_LABELS: Record<string, string> = {
  politics: "سیاسی", economy: "اقتصادی", international: "بین‌الملل", society: "اجتماعی",
  sports: "ورزشی", culture: "فرهنگی", technology: "فناوری", health: "سلامت", general: "عمومی",
};
export const STREAM_LABELS = { all: "همه", domestic: "داخل ایران", foreign: "خارج ایران", manual: "دستی" };
export type Stream = keyof typeof STREAM_LABELS;
export type Role = "owner" | "editor_in_chief" | "staff";
export type Task = { news_id: string; title: string; desk: string; stream: Exclude<Stream, "all">;
  status: string; decision: string | null; version: number | null };
export type Workbench = { role: "editor_in_chief" | "staff"; desks: string[]; items: NewsFeedRecord[];
  tasks: Task[]; scan_limit: number; capabilities: { archive: boolean; analysis: boolean; reports: boolean;
    publishing: boolean; preview: boolean }; metrics_snapshot?: null; review_count?: number };
export type Controls = { revision: number; values: Record<string, boolean>; can_update: boolean };
type Poll = { source_id: string; stream: string; enabled: boolean; last_success_at: string | null;
  consecutive_failures: number | null };
type MonitorStatus = "ok" | "unavailable" | "not_authorized";
export type OwnerData = { health?: Record<string, string>; workflow_counts?: Record<string, number> | null;
  sources?: { registered: number; enabled: number; items: Poll[]; scan_limit: number;
    monitoring: Record<"domestic" | "foreign", MonitorStatus> };
  foreign_monitor?: boolean; schedule?: boolean; publishing?: boolean; controls?: Controls;
  ai_cost?: { committed_usd: number | null; period: string; basis: string };
  audit?: { occurred_at: string; actor_role: string; action: string; decision: string }[] };
export type SavedView = { view_id: string; name: string; filters_json: { desk: string; stream: Stream } };
export const object = (x: unknown): x is Record<string, unknown> => !!x && typeof x === "object" && !Array.isArray(x);
const text = (x: unknown): x is string => typeof x === "string";
const count = (x: unknown): x is number => typeof x === "number" && Number.isSafeInteger(x) && x >= 0;
const date = (x: unknown): x is string => text(x) && Number.isFinite(Date.parse(x));
const desk = (x: unknown): x is string => text(x) && Object.hasOwn(DESK_LABELS, x);
const stream = (x: unknown): x is Stream => text(x) && Object.hasOwn(STREAM_LABELS, x);
const bool = (x: unknown) => typeof x === "boolean";

export function parseRole(x: unknown): Role {
  if (!object(x) || x.schema_version !== "1.0" || !["owner", "editor_in_chief", "staff"].includes(String(x.role)))
    throw new Error("invalid session");
  return x.role as Role;
}
export function parseWorkbench(x: unknown, role: "editor_in_chief" | "staff"): Workbench {
  if (!object(x) || x.schema_version !== "1.0" || x.role !== role ||
      x.scope !== "bounded_authorized_projection" || x.scan_limit !== 200 || !Array.isArray(x.items) ||
      x.items.length > 200 || !Array.isArray(x.desks) || !x.desks.every(desk) ||
      !Array.isArray(x.tasks) || x.tasks.length > x.items.length || !object(x.capabilities))
    throw new Error("invalid workbench");
  const permitted = new Set(x.desks);
  const items = parseNewsFeed({ schema_version: "1.0", items: x.items, count: x.items.length });
  if (items.some(i => !i.desks.length || i.desks.some(d => !permitted.has(d)))) throw new Error("invalid desk scope");
  const ids = new Set(items.map(i => i.news_id));
  if (!x.tasks.every(t => object(t) && text(t.news_id) && ids.has(t.news_id) &&
      text(t.title) && desk(t.desk) && permitted.has(t.desk) && stream(t.stream) && t.stream !== "all" &&
      ["not_started", "draft", "held", "ready_for_policy"].includes(String(t.status)) &&
      (t.decision === null || ["auto_allowed", "human_review", "blocked"].includes(String(t.decision))) &&
      (t.version === null || (count(t.version) && t.version > 0))))
    throw new Error("invalid task");
  const capabilities = x.capabilities;
  if (!["archive", "analysis", "reports", "publishing", "preview"].every(k => bool(capabilities[k])) ||
      (role === "staff" && (capabilities.publishing !== false || "metrics_snapshot" in x || "review_count" in x ||
        "operations" in x || "private_reports" in x))) throw new Error("invalid role scope");
  if (role === "editor_in_chief" && (x.metrics_snapshot !== null || !count(x.review_count))) throw new Error("invalid metrics");
  return { role, desks: x.desks, items, tasks: x.tasks as Task[], scan_limit: 200,
    capabilities: capabilities as Workbench["capabilities"],
    ...(role === "editor_in_chief" ? { metrics_snapshot: null, review_count: x.review_count as number } : {}) };
}
const OUTPUTS = ["public_analysis", "studio", "studio_audio", "studio_video", "publishing",
  "publishing_domestic", "publishing_foreign", "publishing_reports"];
export function parseControls(x: unknown): Controls {
  if (!object(x) || !count(x.revision) || !bool(x.can_update) || !object(x.values) ||
      Object.keys(x.values).length !== OUTPUTS.length || !OUTPUTS.every(k => bool((x.values as Record<string, unknown>)[k])))
    throw new Error("invalid controls");
  return x as unknown as Controls;
}
export function parseOwner(x: unknown): OwnerData {
  if (!object(x) || x.schema_version !== "1.0" || x.role !== "owner" || !object(x.operations))
    throw new Error("invalid owner");
  const v = x.operations;
  if (Object.keys(v).some(k => !["health", "workflow_counts", "sources", "foreign_monitor", "schedule", "controls", "ai_cost", "audit", "publishing"].includes(k)))
    throw new Error("invalid owner scope");
  if (v.controls !== undefined) parseControls(v.controls);
  if (v.health !== undefined && (!object(v.health) || Object.entries(v.health).some(([k, a]) =>
      !["central", "domestic", "foreign"].includes(k) || !["ok", "unavailable"].includes(String(a))))) throw new Error("health");
  if (v.workflow_counts !== undefined && v.workflow_counts !== null &&
      (!object(v.workflow_counts) || Object.values(v.workflow_counts).some(a => !count(a)))) throw new Error("workflow");
  if (v.sources !== undefined && (!object(v.sources) || !count(v.sources.registered) || !count(v.sources.enabled) ||
      v.sources.enabled > v.sources.registered || v.sources.scan_limit !== 200 || !Array.isArray(v.sources.items) ||
      v.sources.items.length > 200 || !v.sources.items.every(a => object(a) && text(a.source_id) &&
        ["domestic", "foreign"].includes(String(a.stream)) && bool(a.enabled) &&
        (a.last_success_at === null || date(a.last_success_at)) &&
        (a.consecutive_failures === null || count(a.consecutive_failures))) ||
      !object(v.sources.monitoring) || Object.keys(v.sources.monitoring).length !== 2 ||
      !["domestic", "foreign"].every(k => ["ok", "unavailable", "not_authorized"].includes(
        String((v.sources!.monitoring as Record<string, unknown>)[k]))))) throw new Error("source health");
  if (v.ai_cost !== undefined && (!object(v.ai_cost) || !text(v.ai_cost.period) ||
      v.ai_cost.basis !== "ledger_estimate_utc_month" ||
      !(v.ai_cost.committed_usd === null || (typeof v.ai_cost.committed_usd === "number" &&
      Number.isFinite(v.ai_cost.committed_usd) && v.ai_cost.committed_usd >= 0)))) throw new Error("cost");
  if (v.audit !== undefined && (!Array.isArray(v.audit) || v.audit.length > 20 ||
      !v.audit.every(a => object(a) && date(a.occurred_at) && text(a.actor_role) && text(a.action) && text(a.decision))))
    throw new Error("audit");
  for (const k of ["schedule", "foreign_monitor", "publishing"]) {
    if (v[k] !== undefined && !bool(v[k])) throw new Error("invalid capability");
  }
  return v as unknown as OwnerData;
}
export function parseViews(x: unknown): SavedView[] {
  if (!object(x) || x.schema_version !== "1.0" || !Array.isArray(x.items) || x.items.length > 10 ||
      !x.items.every(v => object(v) && text(v.view_id) && /^[a-f0-9-]{36}$/.test(v.view_id) &&
        text(v.name) && v.name.length <= 64 && object(v.filters_json) &&
        desk(v.filters_json.desk) && stream(v.filters_json.stream))) throw new Error("invalid views");
  return x.items as SavedView[];
}
export class RequestError extends Error {
  constructor(public readonly status: number) { super("request unavailable"); }
}
export async function request(token: string, path: string, signal: AbortSignal,
                              method = "GET", body?: unknown): Promise<unknown> {
  const r = await fetch(path, { method, signal, credentials: "same-origin", cache: "no-store",
    redirect: "error", headers: { Authorization: "Bearer " + token, Accept: "application/json",
      ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (!r.ok) throw new RequestError(r.status);
  return r.json();
}
type Result<T> = { path: string; attempt: number; data: T | null; error: number | null };
export function useResource<T>(token: string, path: string, parse: (x: unknown) => T) {
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<Result<T> | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    request(token, path, controller.signal).then(parse).then(data => {
      if (!controller.signal.aborted) setResult({ path, attempt, data, error: null });
    }).catch(e => {
      if (!controller.signal.aborted) setResult({ path, attempt, data: null, error: e instanceof RequestError ? e.status : 0 });
    });
    return () => controller.abort();
  }, [token, path, attempt, parse]);
  const current = result?.path === path && result.attempt === attempt ? result : null;
  return { data: current?.data ?? null, error: current?.error ?? null, loading: current === null,
    refresh: () => setAttempt(n => n + 1) };
}
export const parseStaff = (x: unknown) => parseWorkbench(x, "staff");
export const parseExecutive = (x: unknown) => parseWorkbench(x, "editor_in_chief");
export const timeFa = (x: string) => new Date(x).toLocaleString("fa-IR", { timeZone: "Asia/Tehran" });
