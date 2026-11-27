import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import ArchiveSearch, { parseArchive } from "../src/features/archive/ArchiveSearch";
import App, { parseNewsFeed } from "../src/app/App";
import Login from "../src/features/auth/Login";
import ArticleEditor, { parseEditorialPreview } from "../src/features/editor/ArticleEditor";
import DraftWorkspace, { parseWorkspace } from "../src/features/editor/DraftWorkspace";
import NewsDesk from "../src/features/newsroom/NewsDesk";
import SourcesManager, { ForeignMonitor, parseForeignStatus, parseSources } from "../src/features/sources/SourcesManager";
import Settings, { parseDomesticSchedule, scheduleError } from "../src/features/settings/Settings";
import { parseAuthResult } from "../src/lib/auth";

const item = {
  news_id: "NEWS-DB-1", title: "خبر واقعی از API آزمایشی",
  stream: "domestic", source_id: "SRC-1",
  original_url: "https://example.invalid/news/1",
  collected_at: "2026-11-23T07:00:00Z",
  reuse_permission: "unverified", content_scope: "summary",
  event_id: "EVENT-1", desks: ["economy"],
} as const;

const envelope = (items: unknown[]) => ({ schema_version: "1.0", count: items.length, items });

describe("authenticated newsroom projection", () => {
  it("accepts the existing real API envelope and renders source and title", () => {
    const items = parseNewsFeed(envelope([{ ...item, desks: [...item.desks] }]));
    const html = renderToStaticMarkup(createElement(NewsDesk, { items }));
    expect(html).toContain(item.title);
    expect(html).toContain(item.original_url);
    expect(html).toContain("EVENT-1");
    expect(html).not.toContain("منبع نمونه");
  });

  it("rejects malformed, synthetic-only and duplicate responses", () => {
    expect(() => parseNewsFeed({ mode: "synthetic", items: [item] })).toThrow();
    expect(() => parseNewsFeed(envelope([{ ...item, collected_at: "invalid" }]))).toThrow();
    expect(() => parseNewsFeed(envelope([item, item]))).toThrow();
    expect(() => parseNewsFeed({ ...envelope([item]), count: 0 })).toThrow();
  });

  it("renders empty and error states without showing stale news", () => {
    expect(renderToStaticMarkup(createElement(NewsDesk, { items: [] }))).toContain("خبری نیست");
    const items = parseNewsFeed(envelope([item]));
    for (const state of ["loading", "error", "forbidden"] as const) {
      const html = renderToStaticMarkup(createElement(NewsDesk, { items, state }));
      expect(html).not.toContain(item.title);
    }
  });

  it("requires a session before showing the feed", () => {
    const html = renderToStaticMarkup(createElement(App));
    expect(html).toContain("در حال بررسی وضعیت ورود");
    expect(html).not.toContain("session-token");
    expect(html).not.toContain(item.title);
    expect(html).not.toContain("داده‌های ساختگی");
  });

  it("does not render non-HTTP source links", () => {
    const items = parseNewsFeed(envelope([{ ...item, original_url: "javascript:alert(1)" }]));
    const html = renderToStaticMarkup(createElement(NewsDesk, { items }));
    expect(html).not.toContain("javascript:");
    expect(html).toContain("نشانی منبع نامعتبر");
  });
});

describe("staff session", () => {
  it("renders a personal credential form without prefilled credentials", () => {
    const html = renderToStaticMarkup(createElement(Login, { onSession: () => {} }));
    expect(html).toContain("login-username");
    expect(html).toContain("login-password");
    expect(html).not.toContain("session-token");
  });

  it("does not accept a session before required reset", () => {
    expect(parseAuthResult({ schema_version: "1.0", must_reset: true })).toEqual({ mustReset: true });
    expect(() => parseAuthResult({
      schema_version: "1.0", must_reset: true, access_token: "fixture",
    })).toThrow();
    expect(() => parseAuthResult({
      schema_version: "1.0", must_reset: false, token_type: "bearer", access_token: "fixture", expires_in: 9999,
    })).toThrow();
    expect(parseAuthResult({
      schema_version: "1.0", must_reset: false, token_type: "bearer", access_token: "fixture", expires_in: 900,
    })).toEqual({ mustReset: false, token: "fixture", expiresIn: 900 });
  });
});

describe("source registry", () => {
  const domestic = {
    source_id: "SRC-IR-1", site_url: "https://example.invalid",
    feed_url: "https://example.invalid/rss", country: "IR", language: "fa",
    stream: "domestic", license_scope: "summary", enabled: true,
  } as const;
  const foreignCandidate = {
    ...domestic, source_id: "SRC-DE-1", country: "DE",
    stream: "foreign", enabled: false,
  } as const;

  it("renders approved domestic sources and keeps foreign candidates inactive", () => {
    const sources = parseSources({
      schema_version: "1.0", sources: [foreignCandidate, domestic],
    });
    const html = renderToStaticMarkup(createElement(SourcesManager, { sources }));
    expect(html).toContain("SRC-IR-1");
    expect(html).toContain("فعال · دامنه حق استفاده");
    expect(html).toContain("نامزد خارجی غیرفعال");
  });

  it("rejects malformed, duplicate and non-http source records", () => {
    expect(() => parseSources({ schema_version: "1.0", sources: [
      { ...domestic, site_url: "javascript:alert(1)" },
    ] })).toThrow();
    expect(() => parseSources({
      schema_version: "1.0", sources: [domestic, domestic],
    })).toThrow();
    expect(() => parseSources({ schema_version: "1.0", sources: [
      { ...domestic, enabled: "yes" },
    ] })).toThrow();
    expect(() => parseSources({ schema_version: "1.0", sources: [
      { ...domestic, site_url: "http://" },
    ] })).toThrow();
    const { feed_url: _omitted, ...withoutFeed } = domestic;
    expect(parseSources({ schema_version: "1.0", sources: [withoutFeed] })).toHaveLength(1);
  });

  it("does not show source data when access is denied", () => {
    const html = renderToStaticMarkup(createElement(SourcesManager, {
      sources: [domestic], state: "forbidden",
    }));
    expect(html).toContain("دسترسی به دفتر منابع مجاز نیست");
    expect(html).not.toContain("SRC-IR-1");
  });
});

describe("domestic ingestion settings", () => {
  const config = {
    schema_version: "1.0", contract_name: "schedule_config", config_id: "SCH-DOMESTIC-INGEST",
    scope: "domestic", revision: 2,
    value: { schema_version: "1.0", config_id: "SCH-DOMESTIC-INGEST", stream: "domestic",
      desk: "all", timezone: "Asia/Tehran", mode: "interval", fixed_times_local: [],
      interval_minutes: 7, enabled: false, updated_at: "2026-11-26T08:00:00Z" },
  };
  it("accepts only scoped and bounded ingestion settings", () => {
    expect(parseDomesticSchedule(config)).toEqual({ revision: 2, minutes: 7, enabled: false });
    expect(() => parseDomesticSchedule({ ...config, scope: "foreign" })).toThrow();
    expect(() => parseDomesticSchedule({ ...config, value: { ...config.value, interval_minutes: 0 } })).toThrow();
    expect(() => parseDomesticSchedule({ ...config, value: { ...config.value, stream: "foreign" } })).toThrow();
  });
  it("handles denied access and revision conflict independently", () => {
    expect(scheduleError(403)).toBe("forbidden");
    expect(scheduleError(409)).toBe("conflict");
    const html = renderToStaticMarkup(createElement(Settings, { token: "fixture-not-a-real-token" }));
    expect(html).toContain("در حال دریافت تنظیمات");
    expect(html).not.toContain("fixture-not-a-real-token");
    expect(html).not.toContain("ذخیره تنظیمات");
  });
});

describe("foreign monitor", () => {
  const status = { schema_version: "1.0", mode: "fixture", candidate_count: 1,
    verified_candidate_count: 1, raw_count: 1, delivered_count: 1,
    received_count: 1, processed_count: 1,
    candidates: [{ source_id: "SRC-FIXTURE", country: "DE", language: "de", verified: true, enabled: false }],
    polls: [{ source_id: "SRC-FIXTURE", revision: 1, last_success_at: null,
      next_poll_at: "2026-11-26T19:00:00Z", consecutive_failures: 0 }],
  };
  it("parses actual fixture-backed status and never enables a candidate", () => {
    expect(parseForeignStatus(status).processed_count).toBe(1);
    expect(() => parseForeignStatus({ ...status, mode: "live" })).toThrow();
    expect(() => parseForeignStatus({ ...status, processed_count: 2 })).toThrow();
    expect(() => parseForeignStatus({ ...status, candidates: [{ ...status.candidates[0], enabled: true }] })).toThrow();
  });
  it("does not show candidate data before the authorized response", () => {
    const html = renderToStaticMarkup(createElement(ForeignMonitor, { token: "fixture-not-real" }));
    expect(html).toContain("جهان‌بان");
    expect(html).not.toContain("SRC-FIXTURE");
    expect(html).not.toContain("fixture-not-real");
  });
});

describe("editorial preview", () => {
  const response = {
    schema_version: "1.0",
    rules_version: "RULE-UI-PREVIEW:1",
    original: "عبارت قدیمی",
    proposed: "عبارت مصوب",
    replacements: [{
      rule_id: "RULE-UI-1",
      original: "عبارت قدیمی",
      proposed: "عبارت مصوب",
      action: "applied",
      reason: "UNAMBIGUOUS_DRAFT_RULE",
    }],
    quality: {
      passed: true,
      reason_codes: ["QUALITY_CHECKS_PASSED"],
      evidence_refs: ["NEWS-FIXTURE", "quality:fixture"],
      source_hash: "a".repeat(64),
      draft_hash: "b".repeat(64),
    },
  } as const;

  it("parses a backend quality result and rejects malformed evidence", () => {
    expect(parseEditorialPreview(response).proposed).toBe("عبارت مصوب");
    expect(() => parseEditorialPreview({
      ...response,
      quality: { ...response.quality, reason_codes: [false] },
    })).toThrow();
    expect(() => parseEditorialPreview({
      ...response,
      replacements: [{ ...response.replacements[0], action: "auto_allowed" }],
    })).toThrow();
  });

  it("starts empty and never exposes the session token in markup", () => {
    const html = renderToStaticMarkup(createElement(ArticleEditor, {
      token: "fixture-not-a-real-token",
    }));
    expect(html).toContain("کنترل واژه‌نامه و کیفیت");
    expect(html).toContain("پیش‌نمایش بدون انتشار");
    expect(html).not.toContain("fixture-not-a-real-token");
    expect(html).not.toContain("عبارت مصوب");
  });
});

describe("source-linked durable drafts", () => {
  const snapshot = { news_id: item.news_id, original_title: "Fixture", original_text: "Immutable source",
    stream: "foreign", language: "en" };
  const response = { schema_version: "1.0", mode: "manual", source: snapshot,
    source_hash: "a".repeat(64), desks: ["international"], latest_version: 0,
    saved: null, history: [] };
  it("accepts only a source-linked, versioned workspace", () => {
    expect(parseWorkspace(response, item.news_id).source.original_text).toBe("Immutable source");
    expect(() => parseWorkspace(response, "NEWS-OTHER")).toThrow();
    expect(() => parseWorkspace({ ...response, source_hash: "bad" }, item.news_id)).toThrow();
    expect(parseWorkspace({ ...response, ai_available: true, ai_desks: ["international"],
      generation_mode: "fixture" }, item.news_id).generation_mode).toBe("fixture");
    expect(() => parseWorkspace({ ...response, ai_available: true, ai_desks: [],
      generation_mode: "fixture" }, item.news_id)).toThrow();
    const saved = { version: 1, source_hash: response.source_hash, source_snapshot: snapshot,
      article: { news_id: item.news_id, desk: "international", status: "draft",
        title: "title", lead: "lead", body: "body", short_summary: "summary" },
      translation: null, quality: { passed: true, reason_codes: [] },
      policy: { decision: "human_review", reason_codes: ["SOURCE_RIGHTS_UNKNOWN"] } };
    expect(parseWorkspace({ ...response, latest_version: 1, saved }, item.news_id).saved?.version).toBe(1);
    expect(() => parseWorkspace({ ...response, latest_version: 1,
      saved: { ...saved, policy: { decision: "auto_allowed", reason_codes: [] } } }, item.news_id)).toThrow();
  });
  it("renders real feed selection without exposing the token", () => {
    const html = renderToStaticMarkup(createElement(DraftWorkspace, {
      token: "fixture-not-a-real-token", items: parseNewsFeed(envelope([{ ...item, desks: [...item.desks] }])),
    }));
    expect(html).toContain("پیش‌نویس و تاریخچه خبر");
    expect(html).toContain(item.title);
    expect(html).not.toContain("fixture-not-a-real-token");
    expect(html).not.toContain("ذخیره نسخه جدید");
  });
});

describe("archive search", () => {
  it("accepts bounded server results and rejects a private stream or inconsistent count", () => {
    const row = { news_id: "NEWS-ARCHIVE", title: "خبر", stream: "foreign", source_id: "SRC-ARCHIVE",
      received_at: "2026-11-27T00:00:00Z" };
    expect(parseArchive({ schema_version: "1.0", count: 1, items: [row] })).toHaveLength(1);
    expect(() => parseArchive({ schema_version: "1.0", count: 0, items: [row] })).toThrow();
    expect(() => parseArchive({ schema_version: "1.0", count: 1, items: [{ ...row, stream: "executive_private" }] })).toThrow();
    const html = renderToStaticMarkup(createElement(ArchiveSearch, { token: "fixture-not-real" }));
    expect(html).toContain("جست‌وجو در آرشیو مجاز");
    expect(html).not.toContain("fixture-not-real");
  });
});
