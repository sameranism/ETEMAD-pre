import { useEffect } from "react";
import ExecutiveDashboard from "../executive/ExecutiveDashboard";
import OwnerDashboard from "../owner/OwnerDashboard";
import StaffDashboard from "../staff/StaffDashboard";
import { parseRole, useResource } from "./data";
export default function DashboardSession({ token, onUnauthorized }: { token: string; onUnauthorized: () => void }) {
  const state = useResource(token, "/v1/ui/session", parseRole);
  useEffect(() => { if (state.error === 401) onUnauthorized(); }, [state.error, onUnauthorized]);
  if (state.loading) return <p role="status" className="message">در حال احراز نقش و سطح دسترسی…</p>;
  if (state.data === "owner") return <OwnerDashboard token={token} />;
  if (state.data === "editor_in_chief") return <ExecutiveDashboard token={token} />;
  if (state.data === "staff") return <StaffDashboard token={token} />;
  return <section className="message" role="alert">نشست یا نقش مجاز در دسترس نیست.
    <button type="button" onClick={state.refresh}>تلاش مجدد</button></section>;
}
