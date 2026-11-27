import type { NewsFeedRecord } from "../features/newsroom/NewsDesk";

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNewsItem(value: unknown): value is NewsFeedRecord {
  if (!isObject(value)) return false;
  return typeof value.news_id === "string" && value.news_id.length > 0 &&
    typeof value.title === "string" &&
    typeof value.source_id === "string" &&
    typeof value.original_url === "string" &&
    typeof value.collected_at === "string" &&
    Number.isFinite(Date.parse(value.collected_at)) &&
    (value.stream === "domestic" || value.stream === "foreign" || value.stream === "manual") &&
    (value.reuse_permission === "unverified" || value.reuse_permission === "licensed" ||
      value.reuse_permission === "link_only" || value.reuse_permission === "blocked") &&
    (value.content_scope === "metadata" || value.content_scope === "summary" ||
      value.content_scope === "full_text") &&
    typeof value.event_id === "string" &&
    Array.isArray(value.desks) && value.desks.every((desk: unknown) => typeof desk === "string");
}

export function parseNewsFeed(value: unknown): NewsFeedRecord[] {
  if (!isObject(value) || value.schema_version !== "1.0" ||
      !Array.isArray(value.items) || value.count !== value.items.length ||
      !value.items.every(isNewsItem)) {
    throw new Error("Invalid newsroom response");
  }
  const items: NewsFeedRecord[] = value.items;
  if (new Set(items.map((item) => item.news_id)).size !== items.length) {
    throw new Error("Duplicate newsroom identity");
  }
  return items;
}

