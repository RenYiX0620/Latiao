import { useState, useEffect, useCallback } from "react";
import { fetch } from "@tauri-apps/plugin-http";

const SIDECAR = "http://127.0.0.1:8000";

export function useCronJobs(showToast: (msg: string) => void) {
  const [cronJobs, setCronJobs] = useState<{id: string; schedule: string; task: string; enabled: boolean; action: string}[]>([]);
  const [newCron, setNewCron] = useState({ schedule: "0 9 * * *", task: "", action: "notify" });

  useEffect(() => {
    (async () => {
      try {
        const resp = await fetch(SIDECAR + "/v1/cron");
        const data = await resp.json();
        if (data.status === "ok") setCronJobs(data.jobs || []);
      } catch (e) { console.error(e); }
    })();
  }, []);

  const addCronJob = useCallback(async () => {
    if (!newCron.task.trim()) { showToast("请输入任务描述"); return; }
    try {
      const resp = await fetch(SIDECAR + "/v1/cron", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(newCron),
      });
      const data = await resp.json();
      if (data.status === "ok") {
        setCronJobs(prev => [...prev, data.job]);
        setNewCron({ schedule: "0 9 * * *", task: "", action: "notify" });
        showToast("定时任务已创建");
      }
    } catch (e) { console.error(e); showToast("创建失败"); }
  }, [newCron, showToast]);

  const toggleCronJob = useCallback(async (jobId: string) => {
    try {
      const resp = await fetch(`${SIDECAR}/v1/cron/${jobId}/toggle`, { method: "POST" });
      const data = await resp.json();
      if (data.status === "ok") setCronJobs(prev => prev.map(j => j.id === jobId ? data.job : j));
    } catch (e) { console.error(e); }
  }, []);

  const deleteCronJob = useCallback(async (jobId: string) => {
    try {
      await fetch(`${SIDECAR}/v1/cron/${jobId}`, { method: "DELETE" });
      setCronJobs(prev => prev.filter(j => j.id !== jobId));
      showToast("任务已删除");
    } catch (e) { console.error(e); }
  }, [showToast]);

  return { cronJobs, newCron, setNewCron, addCronJob, toggleCronJob, deleteCronJob };
}
