import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import OwnerDashboard, { OwnerOverview } from "../src/features/owner/OwnerDashboard";
import ExecutiveDashboard, { ExecutiveOverview } from "../src/features/executive/ExecutiveDashboard";
import StaffDashboard, { StaffOverview } from "../src/features/staff/StaffDashboard";
import { parseExecutive, parseOwner, parseRole, parseStaff, parseViews } from "../src/features/dashboard/data";

const base = { schema_version: "1.0", role: "staff", desks: ["economy"],
  items: [], tasks: [], scope: "bounded_authorized_projection", scan_limit: 200,
  capabilities: { archive: false, analysis: false, reports: false, publishing: false, preview: false } };
describe("P27 distinct role workspaces", () => {
  it("uses only server-returned role and rejects forged or cross-role projections", () => {
    expect(parseRole({ schema_version: "1.0", role: "staff" })).toBe("staff");
    expect(() => parseRole({ schema_version: "1.0", role: "admin" })).toThrow();
    expect(() => parseExecutive(base)).toThrow();
    expect(() => parseStaff({ ...base, operations: {} })).toThrow();
    expect(() => parseStaff({ ...base, private_reports: [] })).toThrow();
    expect(() => parseStaff({ ...base, capabilities: { ...base.capabilities, publishing: true } })).toThrow();
    expect(() => parseOwner({ schema_version: "1.0", role: "owner", operations: { private_reports: [] } })).toThrow();
  });
  it("renders independent role headings and graphite executive workspace", () => {
    const owner = renderToStaticMarkup(createElement(OwnerDashboard, { token: "fixture" }));
    const eic = renderToStaticMarkup(createElement(ExecutiveDashboard, { token: "fixture" }));
    const staff = renderToStaticMarkup(createElement(StaffDashboard, { token: "fixture" }));
    expect(owner).toContain("داشبورد مالک سامانه");
    expect(eic).toContain("executive-dashboard");
    expect(eic).toContain("داشبورد مدیرمسئول");
    expect(staff).toContain("داشبورد کارکنان");
    expect(owner).not.toContain("گزارش‌های محرمانه");
    expect(staff).not.toContain("کلیدهای مستقل خروجی");
    expect(staff).not.toContain("گزارش‌های محرمانه");
  });
  it("renders authorized bounded counts and never fabricates full KPIs", () => {
    const staff = renderToStaticMarkup(createElement(StaffOverview, { data: parseStaff(base) }));
    expect(staff).toContain("اقتصادی");
    expect(staff).not.toContain("گزارش‌های محرمانه");
    const eic = parseExecutive({ ...base, role: "editor_in_chief", metrics_snapshot: null, review_count: 0 });
    expect(renderToStaticMarkup(createElement(ExecutiveOverview, { data: eic }))).toContain("شاخص جامع P17 هنوز در دسترس نیست");
    const ops = parseOwner({ schema_version: "1.0", role: "owner", operations: {
      health: { central: "ok", domestic: "unavailable", foreign: "unavailable" },
      ai_cost: { committed_usd: null, period: "2026-11", basis: "ledger_estimate_utc_month" },
    } });
    expect(renderToStaticMarkup(createElement(OwnerOverview, { data: ops }))).toContain("داده موجود نیست");
  });
  it("rejects malformed or out-of-scope views", () => {
    expect(parseViews({ schema_version: "1.0", items: [] })).toEqual([]);
    expect(() => parseViews({ schema_version: "1.0", items: [
      { view_id: "00000000-0000-0000-0000-000000000001", name: "test", filters_json: { desk: "private", stream: "all" } }
    ] })).toThrow();
  });
});
