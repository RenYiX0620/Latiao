import { useState, useEffect, useCallback } from "react";
import { sidecarFetch, sidecarFetchWithRetry } from "../utils/api";

export function useSkills(showToast: (msg: string) => void) {
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
    if (!key.trim()) { showToast("请输入 API Key"); return; }
    setTavilyKey(prev => ({ ...prev, loading: true }));
    try {
      const data = await sidecarFetch("/v1/settings/tavily-key", "POST", { key });
      if (data.status === "ok") {
        setTavilyKey({ hasKey: true, masked: data.masked, loading: false });
        showToast("Tavily API Key 已保存");
      } else {
        setTavilyKey(prev => ({ ...prev, loading: false }));
        showToast(data.message || "保存失败");
      }
    } catch (e) { console.error("Failed to save Tavily key:", e); setTavilyKey(prev => ({ ...prev, loading: false })); showToast("保存失败"); }
  }, [showToast]);

  const deleteTavilyKey = useCallback(async () => {
    setTavilyKey(prev => ({ ...prev, loading: true }));
    try {
      const data = await sidecarFetch("/v1/settings/tavily-key", "DELETE");
      if (data.status === "ok") {
        setTavilyKey({ hasKey: false, masked: null, loading: false });
        showToast("Tavily API Key 已删除");
      } else {
        setTavilyKey(prev => ({ ...prev, loading: false }));
        showToast(data.message || "删除失败");
      }
    } catch (e) {
      console.error("Failed to delete Tavily key:", e);
      setTavilyKey(prev => ({ ...prev, loading: false }));
      showToast("删除失败");
    }
  }, [showToast]);

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
      showToast("技能已删除");
    } catch (e) { console.error("Failed to delete skill:", e); }
  }, [showToast]);

  const addSkill = useCallback(async () => {
    if (!newSkill.name.trim() || !newSkill.content.trim()) { showToast("请填写技能名称和内容"); return; }
    try {
      const data = await sidecarFetch("/v1/skills", "POST", { name: newSkill.name, content: newSkill.content });
      if (data.status === "ok") {
        setSkills(prev => [...prev, data.skill]);
        setNewSkill({ name: "", content: "" });
        showToast("技能已创建");
      }
    } catch (e) { console.error("Failed to create skill:", e); showToast("创建失败"); }
  }, [newSkill, showToast]);

  return { skills, newSkill, setNewSkill, toggleSkill, deleteSkill, addSkill, tavilyKey, saveTavilyKey, deleteTavilyKey };
}
