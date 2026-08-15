import { useTranslation } from "../i18n";

export interface CronJob {
  id: string;
  schedule: string;
  task: string;
  enabled: boolean;
  action: string;
  last_run?: string;
  last_status?: string;
  last_result?: string;
  running?: boolean;
}

interface CronViewProps {
  cronJobs: CronJob[];
  newCron: { schedule: string; task: string; action: string };
  setNewCron: (c: { schedule: string; task: string; action: string }) => void;
  toggleCronJob: (jobId: string) => void;
  deleteCronJob: (jobId: string) => void;
  runCronJob: (jobId: string) => void;
  addCronJob: () => void;
}

const statusKey = (s?: string) =>
  s === "success" ? "cron.status_success" : s === "error" ? "cron.status_error" : "cron.status_skipped";

export default function CronView({ cronJobs, newCron, setNewCron, toggleCronJob, deleteCronJob, addCronJob, runCronJob }: CronViewProps) {
  const { t } = useTranslation();
  return (
    <>
      <div className="cron-list">
        {cronJobs.map((c) => (
          <div key={c.id} className="cron-item" onClick={() => toggleCronJob(c.id)}>
            <span className="cron-schedule">{c.schedule}</span>
            <span className="cron-task">{c.task}</span>
            <span className={`badge ${c.enabled ? "badge-active" : "badge-inactive"}`}>{c.enabled ? t("cron.running") : t("cron.paused")}</span>
            <button className="btn-icon" style={{ fontSize: 12, flexShrink: 0, color: "var(--text-accent)" }}
              onClick={(e) => { e.stopPropagation(); if (!c.running) runCronJob(c.id); }} title={c.running ? t("cron.running_now") : t("cron.run_now")}>{c.running ? "⏳" : "▶"}</button>
            <button className="btn-icon" style={{ fontSize: 12, marginLeft: "auto", flexShrink: 0, color: "var(--text-muted)" }}
              onClick={(e) => { e.stopPropagation(); deleteCronJob(c.id); }} title={t("cron.delete")}>✕</button>
            {c.running && (
              <div style={{ flexBasis: "100%", fontSize: 10, color: "var(--text-accent)", marginTop: 2 }}>⏳ {t("cron.running_now")}</div>
            )}
            {!c.running && c.last_run ? (
              <div style={{ flexBasis: "100%", fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
                {t("cron.last_run_at", { time: c.last_run.replace("T", " ").slice(5, 16) })}
                {" · "}
                {t(statusKey(c.last_status))}
                {c.last_result ? ` · ${c.last_result.replace(/\s+/g, " ").slice(0, 60)}` : ""}
              </div>
            ) : (
              <div style={{ flexBasis: "100%", fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>{t("cron.never_run")}</div>
            )}
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
