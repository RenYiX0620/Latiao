import { useState, useEffect, useCallback } from "react";
import { sidecarFetch } from "../utils/api";
import { useTranslation } from "../i18n";

export function useCronJobs(showToast: (msg: string) => void) {
  const { t } = useTranslation();
  const [cronJobs, setCronJobs] = useState<{id: string; schedule: string; task: string; enabled: boolean; action: string}[]>([]);
  const [newCron, setNewCron] = useState({ schedule: "0 9 * * *", task: "", action: "notify" });

  useEffect(() => {
    (async () => {
      try {
        const data = await sidecarFetch("/v1/cron");
        if (data.status === "ok") setCronJobs(data.jobs || []);
      } catch (e) { console.error(e); }
    })();
  }, []);

  const addCronJob = useCallback(async () => {
    if (!newCron.task.trim()) { showToast(t("cron.fill_task")); return; }
    try {
      const data = await sidecarFetch("/v1/cron", "POST", newCron);
      if (data.status === "ok") {
        setCronJobs(prev => [...prev, data.job]);
        setNewCron({ schedule: "0 9 * * *", task: "", action: "notify" });
        showToast(t("cron.created"));
      }
    } catch (e) { console.error(e); showToast(t("cron.create_fail")); }
  }, [newCron, showToast, t]);

  const toggleCronJob = useCallback(async (jobId: string) => {
    try {
      const data = await sidecarFetch(`/v1/cron/${jobId}/toggle`, "POST");
      if (data.status === "ok") setCronJobs(prev => prev.map(j => j.id === jobId ? data.job : j));
    } catch (e) { console.error(e); }
  }, []);

  const deleteCronJob = useCallback(async (jobId: string) => {
    try {
      await sidecarFetch(`/v1/cron/${jobId}`, "DELETE");
      setCronJobs(prev => prev.filter(j => j.id !== jobId));
      showToast(t("cron.deleted"));
    } catch (e) { console.error(e); }
  }, [showToast, t]);

  return { cronJobs, newCron, setNewCron, addCronJob, toggleCronJob, deleteCronJob };
}
