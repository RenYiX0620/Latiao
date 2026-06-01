import { useState, useEffect, useCallback } from "react";
import { fetch } from "@tauri-apps/plugin-http";

const SIDECAR = "http://127.0.0.1:8000";

export function useSkills(showToast: (msg: string) => void) {
  const [skills, setSkills] = useState<{name: string; file: string; key: string; enabled: boolean}[]>([]);
  const [newSkill, setNewSkill] = useState({ name: "", content: "" });
  const [tavilyKey, setTavilyKey] = useState({ hasKey: false, masked: null as string | null, loading: false });

  useEffect(() => {
    (async () => {
      try {
        const resp = await fetch(SIDECAR + "/v1/skills");
        const data = await resp.json();
        if (data.status === "ok") setSkills(data.skills || []);
      } catch (e) { console.error(e); }
    })();
  }, []);

  // Fetch Tavily key status
  useEffect(() => {
    (async () => {
      try {
        const resp = await fetch(SIDECAR + "/v1/settings/tavily-key");
        const data = await resp.json();
        if (data.status === "ok") setTavilyKey({ hasKey: data.has_key, masked: data.masked, loading: false });
      } catch (e) { /* endpoint may not exist yet */ }
    })();
  }, []);

  const saveTavilyKey = useCallback(async (key: string) => {
    if (!key.trim()) { showToast("请输入 API Key"); return; }
    setTavilyKey(prev => ({ ...prev, loading: true }));
    try {
      const resp = await fetch(SIDECAR + "/v1/settings/tavily-key", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      const data = await resp.json();
      if (data.status === "ok") {
        setTavilyKey({ hasKey: true, masked: data.masked, loading: false });
        showToast("Tavily API Key 已保存");
      } else {
        setTavilyKey(prev => ({ ...prev, loading: false }));
        showToast(data.message || "保存失败");
      }
    } catch (e) { setTavilyKey(prev => ({ ...prev, loading: false })); showToast("保存失败"); }
  }, [showToast]);

  const deleteTavilyKey = useCallback(async () => {
    setTavilyKey(prev => ({ ...prev, loading: true }));
    try {
      await fetch(SIDECAR + "/v1/settings/tavily-key", { method: "DELETE" });
      setTavilyKey({ hasKey: false, masked: null, loading: false });
      showToast("Tavily API Key 已删除");
    } catch (e) { setTavilyKey(prev => ({ ...prev, loading: false })); }
  }, [showToast]);

  const toggleSkill = useCallback(async (key: string) => {
    try {
      const resp = await fetch(`${SIDECAR}/v1/skills/${key}/toggle`, { method: "POST" });
      const data = await resp.json();
      if (data.status === "ok") setSkills(prev => prev.map(s => s.key === key ? { ...s, enabled: data.enabled } : s));
    } catch (e) { console.error(e); }
  }, []);

  const deleteSkill = useCallback(async (key: string) => {
    try {
      await fetch(`${SIDECAR}/v1/skills/${key}`, { method: "DELETE" });
      setSkills(prev => prev.filter(s => s.key !== key));
      showToast("技能已删除");
    } catch (e) { console.error(e); }
  }, [showToast]);

  const addSkill = useCallback(async () => {
    if (!newSkill.name.trim() || !newSkill.content.trim()) { showToast("请填写技能名称和内容"); return; }
    try {
      const resp = await fetch(SIDECAR + "/v1/skills", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newSkill.name, content: newSkill.content }),
      });
      const data = await resp.json();
      if (data.status === "ok") {
        setSkills(prev => [...prev, data.skill]);
        setNewSkill({ name: "", content: "" });
        showToast("技能已创建");
      }
    } catch (e) { console.error(e); showToast("创建失败"); }
  }, [newSkill, showToast]);

  return { skills, newSkill, setNewSkill, toggleSkill, deleteSkill, addSkill, tavilyKey, saveTavilyKey, deleteTavilyKey };
}
