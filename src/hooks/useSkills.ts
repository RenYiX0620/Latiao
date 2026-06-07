import { useState, useEffect, useCallback } from "react";
import { sidecarFetch, sidecarFetchWithRetry } from "../utils/api";
import { useTranslation } from "../i18n";

export function useSkills(showToast: (msg: string) => void) {
  const { t } = useTranslation();
  const [skills, setSkills] = useState<{name: string; file: string; key: string; enabled: boolean}[]>([]);
  const [newSkill, setNewSkill] = useState({ name: "", content: "" });
  const [tavilyKey, setTavilyKey] = useState({ hasKey: false, masked: null as string | null, loading: false });

  useEffect(() => {
    (async () => {
      try {
        const data = await sidecarFetchWithRetry("/v1/skills");
        if (data.status === "ok") setSkills(data.skills || []);
      } catch (e) { console.error(e); }
    })();
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const data = await sidecarFetchWithRetry("/v1/settings/tavily-key");
        if (data.status === "ok") setTavilyKey({ hasKey: data.has_key, masked: data.masked, loading: false });
      } catch (e) { console.error("Failed to load Tavily key status:", e); }
    })();
  }, []);

  const saveTavilyKey = useCallback(async (key: string) => {
    if (!key.trim()) { showToast(t("skills.tavily_fill_key")); return; }
    setTavilyKey(prev => ({ ...prev, loading: true }));
    try {
      const data = await sidecarFetch("/v1/settings/tavily-key", "POST", { key });
      if (data.status === "ok") {
        setTavilyKey({ hasKey: true, masked: data.masked, loading: false });
        showToast(t("skills.tavily_saved"));
      } else {
        setTavilyKey(prev => ({ ...prev, loading: false }));
        showToast(data.message || t("skills.save_fail"));
      }
    } catch (e) { console.error("Failed to save Tavily key:", e); setTavilyKey(prev => ({ ...prev, loading: false })); showToast(t("skills.save_fail")); }
  }, [showToast, t]);

  const deleteTavilyKey = useCallback(async () => {
    setTavilyKey(prev => ({ ...prev, loading: true }));
    try {
      const data = await sidecarFetch("/v1/settings/tavily-key", "DELETE");
      if (data.status === "ok") {
        setTavilyKey({ hasKey: false, masked: null, loading: false });
        showToast(t("skills.tavily_deleted"));
      } else {
        setTavilyKey(prev => ({ ...prev, loading: false }));
        showToast(data.message || t("skills.delete_fail"));
      }
    } catch (e) {
      console.error("Failed to delete Tavily key:", e);
      setTavilyKey(prev => ({ ...prev, loading: false }));
      showToast(t("skills.delete_fail"));
    }
  }, [showToast, t]);

  const toggleSkill = useCallback(async (key: string) => {
    try {
      const data = await sidecarFetch(`/v1/skills/${key}/toggle`, "POST");
      if (data.status === "ok") setSkills(prev => prev.map(s => s.key === key ? { ...s, enabled: data.enabled } : s));
    } catch (e) { console.error("Failed to toggle skill:", e); }
  }, []);

  const deleteSkill = useCallback(async (key: string) => {
    try {
      await sidecarFetch(`/v1/skills/${key}`, "DELETE");
      setSkills(prev => prev.filter(s => s.key !== key));
      showToast(t("skills.deleted"));
    } catch (e) { console.error("Failed to delete skill:", e); }
  }, [showToast, t]);

  const addSkill = useCallback(async () => {
    if (!newSkill.name.trim() || !newSkill.content.trim()) { showToast(t("skills.fill")); return; }
    try {
      const data = await sidecarFetch("/v1/skills", "POST", { name: newSkill.name, content: newSkill.content });
      if (data.status === "ok") {
        setSkills(prev => [...prev, data.skill]);
        setNewSkill({ name: "", content: "" });
        showToast(t("skills.created"));
      }
    } catch (e) { console.error("Failed to create skill:", e); showToast(t("skills.create_fail")); }
  }, [newSkill, showToast, t]);

  return { skills, newSkill, setNewSkill, toggleSkill, deleteSkill, addSkill, tavilyKey, saveTavilyKey, deleteTavilyKey };
}
