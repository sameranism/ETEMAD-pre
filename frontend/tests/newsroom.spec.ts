import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import App, { parseNewsFeed } from "../src/app/App";
import Login from "../src/features/auth/Login";
import NewsDesk from "../src/features/newsroom/NewsDesk";
import SourcesManager, { parseSources } from "../src/features/sources/SourcesManager";
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
