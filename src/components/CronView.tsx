import { useTranslation } from "../i18n";

interface CronViewProps {
  cronJobs: { id: string; schedule: string; task: string; enabled: boolean; action: string }[];
  newCron: { schedule: string; task: string; action: string };
  setNewCron: (c: { schedule: string; task: string; action: string }) => void;
  toggleCronJob: (jobId: string) => void;
  deleteCronJob: (jobId: string) => void;
  addCronJob: () => void;
}

export default function CronView({ cronJobs, newCron, setNewCron, toggleCronJob, deleteCronJob, addCronJob }: CronViewProps) {
  const { t } = useTranslation();
  return (
    <>
      <div className="cron-list">
        {cronJobs.map((c) => (
          <div key={c.id} className="cron-item" onClick={() => toggleCronJob(c.id)}>
            <span className="cron-schedule">{c.schedule}</span>
            <span className="cron-task">{c.task}</span>
            <span className={`badge ${c.enabled ? "badge-active" : "badge-inactive"}`}>{c.enabled ? t("cron.running") : t("cron.paused")}</span>
            <button className="btn-icon" style={{ fontSize: 12, marginLeft: "auto", flexShrink: 0, color: "var(--text-muted)" }}
              onClick={(e) => { e.stopPropagation(); deleteCronJob(c.id); }} title={t("cron.delete")}>✕</button>
          </div>
        ))}
      </div>
      <div style={{ marginTop: 16, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <input className="form-input" style={{ flex: 1, minWidth: 120, margin: 0, fontSize: 11, padding: "6px 10px" }}
          placeholder={t("cron.task_placeholder")} value={newCron.task}
          onChange={e => setNewCron({ ...newCron, task: e.target.value })}
          onKeyDown={e => { if (e.key === "Enter") addCronJob(); }} />
        <input className="form-input" style={{ width: 100, margin: 0, fontSize: 11, padding: "6px 10px", fontFamily: "var(--font-mono)" }}
          placeholder="0 9 * * *" value={newCron.schedule}
          onChange={e => setNewCron({ ...newCron, schedule: e.target.value })} />
        <button className="btn btn-sm btn-primary" onClick={addCronJob}>{t("cron.new_btn")}</button>
      </div>
      <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 8 }}>
        {t("cron.format_hint")}
      </div>
    </>
  );
}
