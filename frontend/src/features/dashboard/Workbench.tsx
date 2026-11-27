import ArticleEditor from "../editor/ArticleEditor";
import DraftWorkspace from "../editor/DraftWorkspace";
import ArchiveSearch from "../archive/ArchiveSearch";
import PoliticalAnalysis from "../analysis/PoliticalAnalysis";
import NewsDesk from "../newsroom/NewsDesk";
import PublishingCenter from "../publishing/PublishingCenter";
import DailyReports from "../reports/DailyReports";
import { DESK_LABELS, STREAM_LABELS, type Task, type Workbench } from "./data";
const STATES: Record<string, string> = { not_started: "در انتظار کار", draft: "پیش‌نویس", held: "متوقف", ready_for_policy: "آماده سیاست" };
const DECISIONS: Record<string, string> = { human_review: "نیازمند بررسی", blocked: "مسدود", auto_allowed: "مجاز طبق سیاست" };
export function TaskQueue({ tasks, executive = false }: { tasks: Task[]; executive?: boolean }) {
  const rows = executive ? tasks.filter(t => t.decision === "human_review" || t.decision === "blocked") : tasks;
  return <section className="dash-panel" aria-label={executive ? "صف تصمیم‌ها" : "وظایف و وضعیت تأیید"}>
    <h2>{executive ? "صف تصمیم‌های تحریریه" : "وظایف و وضعیت تأیید"}</h2>
    <p className="dash-note">وضعیت ثبت‌شده در سامانه؛ تأیید نهایی یا انتشار از این جدول انجام نمی‌شود.</p>
    {!rows.length ? <p>مورد قابل مشاهده در محدوده مجاز نیست.</p> :
      <div className="dash-table"><table><thead><tr><th>خبر</th><th>سرویس</th><th>جریان</th><th>وضعیت</th><th>تصمیم سیاست</th></tr></thead>
      <tbody>{rows.map(t => <tr key={t.news_id}><td dir="auto">{t.title}</td><td>{DESK_LABELS[t.desk]}</td>
        <td>{STREAM_LABELS[t.stream]}</td><td>{STATES[t.status] ?? t.status}</td>
        <td>{t.decision ? DECISIONS[t.decision] : "ثبت نشده"}</td></tr>)}</tbody></table></div>}
  </section>;
}
export default function WorkbenchPanels({ token, data }: { token: string; data: Workbench }) {
  const tasks = new Set(data.tasks.map(t => t.news_id));
  return <>
    <NewsDesk items={data.items} />
    {data.capabilities.preview && <ArticleEditor token={token} />}
    {tasks.size > 0 && <DraftWorkspace token={token} items={data.items.filter(i => tasks.has(i.news_id))} />}
    {data.capabilities.archive && <ArchiveSearch token={token} />}
    {data.capabilities.analysis && <PoliticalAnalysis token={token} />}
    {data.capabilities.reports && <DailyReports token={token} visibility="public" />}
    {data.role === "editor_in_chief" && data.capabilities.publishing && <PublishingCenter token={token} />}
  </>;
}
