import DailyReports from "../reports/DailyReports";
import WorkbenchPanels, { TaskQueue } from "../dashboard/Workbench";
import { parseExecutive, useResource, type Workbench } from "../dashboard/data";
export function ExecutiveOverview({ data }: { data: Workbench }) {
  return <>
    <div className="dash-kpis" aria-label="شاخص‌های محدوده مجاز">
      <div><span>خبرهای قابل مشاهده</span><strong>{data.items.length.toLocaleString("fa-IR")}</strong></div>
      <div><span>کارهای قابل بررسی</span><strong>{data.tasks.length.toLocaleString("fa-IR")}</strong></div>
      <div><span>نیازمند تصمیم</span><strong>{(data.review_count ?? 0).toLocaleString("fa-IR")}</strong></div>
    </div>
    <p className="dash-note">این شمارش فقط از حداکثر ۲۰۰ خبر اخیر با مجوز شماست؛ شاخص جامع P17 هنوز در دسترس نیست.</p>
    <TaskQueue tasks={data.tasks} executive />
  </>;
}
export default function ExecutiveDashboard({ token }: { token: string }) {
  const q = useResource(token, "/v1/ui/editor-dashboard", parseExecutive);
  return <div className="role-dashboard executive-dashboard">
    <header className="dash-header"><div><p className="eyebrow">دیدبان · مدیریت تحریریه</p><h1>داشبورد مدیرمسئول</h1>
      <p>تصمیم‌گیری، عملکرد تحریریه و گزارش‌های محدود به مجوز شخصی</p></div>
      <button type="button" onClick={q.refresh}>به‌روزرسانی</button></header>
    {q.loading && <p role="status">در حال دریافت داده‌های مجاز…</p>}
    {q.error !== null && <p role="alert">داشبورد مجاز در دسترس نیست.</p>}
    {q.data && <>
      <ExecutiveOverview data={q.data} />
      {q.data.capabilities.reports && <section className="dash-panel private-section">
        <h2>مخزن اختصاصی مدیرمسئول</h2><p>فقط سه گزارش محرمانه با مجوز صریح؛ بدون پیش‌خوانی محتوا.</p>
        <DailyReports token={token} visibility="private" />
      </section>}
      <WorkbenchPanels token={token} data={q.data} />
    </>}
  </div>;
}
