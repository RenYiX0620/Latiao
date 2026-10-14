/** 本地模型管理（下载 / 启停 / 上下文 / HF 搜索）。从 App.tsx 拆出（2026-09-24）。 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { DownloadState, HFModelResult, LLMStatus } from "../types";
import { authFetch } from "../utils/api";

interface Deps {
  showToast: (msg: string, type?: string) => void;
  t: (key: string, params?: Record<string, string | number>) => string;
  setSelectedModel: (m: string) => void;
}

export function useLocalLlm({ showToast, t, setSelectedModel }: Deps) {
  const [localModelId, setLocalModelId] = useState("");
  const [setupCheck, setSetupCheck] = useState<{ready: boolean; ok: {item: string; status: string}[]; issues: {item: string; status: string; fix: string}[]} | null>(null);
  const [hfSearch, setHfSearch] = useState("");
  const [hfResults, setHfResults] = useState<HFModelResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [downloadProgress, setDownloadProgress] = useState<Record<string, DownloadState>>({});
  const downloadProgressRef = useRef(downloadProgress);
  useEffect(() => { downloadProgressRef.current = downloadProgress; }, [downloadProgress]);
  const [fixing, setFixing] = useState("");
  const [contextLimit, setContextLimit] = useState(8192);
  const [contextEstimate, setContextEstimate] = useState<{max_context: number; recommended_context: number; ram_available_gb: number; memory_for_context_gb: number} | null>(null);
  const [localLLMStatus, setLocalLLMStatus] = useState<LLMStatus>({ backend: "", status: "checking", model_id: "", model_name: "", port: 1235, message: "", has_image_support: false, token_limit: 32768 });

  const fetchContextEstimate = async (modelPath?: string) => {
    try {
      const params = modelPath ? `?model_path=${encodeURIComponent(modelPath)}` : "";
      const resp = await authFetch("/v1/local-llm/estimate-context" + params);
      const data = await resp.json();
      if (data.max_context) setContextEstimate(data);
      if (data.current_context) {
        setContextLimit(data.current_context);
        contextLimitCommittedRef.current = data.current_context;
      }
    } catch { /* ignore */ }
  };
  useEffect(() => {
    const id = setTimeout(() => { void fetchContextEstimate(); }, 0);
    return () => clearTimeout(id);
  }, []);

  // Set context limit — local state updates immediately (smooth slider), the
  // POST is debounced 300ms, and a failed POST rolls back to the last value the
  // server actually accepted.
  const contextLimitCommittedRef = useRef(contextLimit);
  const contextLimitTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const updateContextLimit = (limit: number) => {
    setContextLimit(limit);
    if (contextLimitTimerRef.current) clearTimeout(contextLimitTimerRef.current);
    contextLimitTimerRef.current = setTimeout(async () => {
      try {
        const resp = await authFetch("/v1/local-llm/context-limit", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ limit }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        contextLimitCommittedRef.current = limit;
      } catch {
        setContextLimit(contextLimitCommittedRef.current);
      }
    }, 300);
  };
  useEffect(() => () => { if (contextLimitTimerRef.current) clearTimeout(contextLimitTimerRef.current); }, []);

  // Fetch setup check on mount
  const fetchSetup = () => {
    authFetch("/v1/local-llm/setup").then(r => r.json()).then(d => setSetupCheck(d)).catch((e) => console.warn("Setup check failed", e));
  };
  useEffect(() => { fetchSetup(); }, []);

  const runFix = async (fixType: string, fixPkg: string) => {
    setFixing(fixPkg);
    try {
      const resp = await authFetch("/v1/local-llm/fix", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fix_type: fixType, fix_pkg: fixPkg }),
      });
      const data = await resp.json();
      showToast(data.status === "ok" ? t("toast.fix_ok") : (data.message || t("toast.fix_fail")));
      // Re-run setup check after fix
      setTimeout(fetchSetup, 2000);
    } catch (e) { console.error(e); showToast(t("toast.fix_req_fail")); }
    setFixing("");
  };

  // ── Download progress polling (like LM Studio) ──
  // Single writer for downloadProgress. Always fetches once on mount; the 2s
  // interval only keeps polling while at least one download is in progress.
  useEffect(() => {
    const poll = async () => {
      try {
        const resp = await authFetch("/v1/local-llm/downloads", { signal: AbortSignal.timeout(5000) });
        const data = await resp.json();
        if (data.status === "ok" && Array.isArray(data.downloads)) {
          const progress: Record<string, DownloadState> = {};
          for (const dl of data.downloads as (DownloadState & { name?: string })[]) {
            const key = dl.model_id || dl.name || JSON.stringify(dl).slice(0, 40);
            if (key) progress[key] = dl;
          }
          void Promise.resolve().then(() => setDownloadProgress(progress));
        }
      } catch { /* ignore poll errors */ }
    };
    poll();
    const interval = setInterval(() => {
      const active = Object.values(downloadProgressRef.current).some((d) => d.status === "downloading");
      if (active) poll();
    }, 2000);
    return () => clearInterval(interval);
  }, []);

  // Monotonic request id so only the latest search response is applied
  const searchReqRef = useRef(0);
  const searchHF = useCallback(async (query?: string, library?: string) => {
    const q = query ?? hfSearch;
    const reqId = ++searchReqRef.current;
    setSearching(true);
    try {
      const libParam = library ? `&library=${encodeURIComponent(library)}` : "";
      const resp = await authFetch(`/v1/local-llm/search?q=${encodeURIComponent(q)}&limit=30${libParam}`, { signal: AbortSignal.timeout(5000) });
      const data = await resp.json();
      if (reqId === searchReqRef.current && data.status === "ok") setHfResults(data.results);
    } catch (e) { console.error(e) }
    if (reqId === searchReqRef.current) setSearching(false);
  }, [hfSearch]);

  // Auto-search with debounce as user types
  useEffect(() => {
    if (!hfSearch.trim()) {
      void Promise.resolve().then(() => setHfResults([]));
      return;
    }
    const timer = setTimeout(() => searchHF(hfSearch), 400);
    return () => clearTimeout(timer);
  }, [hfSearch, searchHF]);

  const downloadModel = async (modelId: string) => {
    showToast(t("toast.dl_start", { name: modelId.split("/").pop() || modelId }));
    try {
      const resp = await authFetch("/v1/local-llm/download", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: modelId }),
      });
      const data = await resp.json();
      if (data.status === "ok") {
        // Immediately fetch downloads to show UI feedback
        const dlResp = await authFetch("/v1/local-llm/downloads");
        const dlData = await dlResp.json();
        if (dlData.status === "ok" && Array.isArray(dlData.downloads)) {
          const progress: Record<string, DownloadState> = {};
          for (const dl of dlData.downloads as (DownloadState & { name?: string })[]) {
            const key = dl.model_id || dl.name || JSON.stringify(dl).slice(0, 40);
            if (key) progress[key] = dl;
          }
          setDownloadProgress(prev => ({ ...prev, ...progress }));
        }
      } else {
        showToast(t("toast.dl_fail") + ": " + (data.message || ""));
      }
    } catch (e) { console.error(e); showToast(t("toast.dl_fail")); }
  };

  const pauseDownload = async (modelId: string) => {
    try {
      await authFetch("/v1/local-llm/download/pause", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: modelId }),
      });
    } catch { showToast(t("app.backend_no_reply"), "warn"); }
  };

  const resumeDownload = async (modelId: string) => {
    try {
      await authFetch("/v1/local-llm/download/resume", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: modelId }),
      });
    } catch { showToast(t("app.backend_no_reply"), "warn"); }
  };

  const cancelDownload = async (modelId: string) => {
    try {
      await authFetch("/v1/local-llm/download/cancel", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: modelId }),
      });
    } catch { showToast(t("app.backend_no_reply"), "warn"); }
  };

  // 注意：selectModelAndLoad 依赖它，声明顺序保持在前面
  const startLocalLLM = async (modelId?: string) => {
    const mid = (modelId || localModelId).trim();
    if (!mid) { showToast(t("toast.need_model_id")); return; }
    if (modelId) setLocalModelId(modelId);
    try {
      setLocalLLMStatus(prev => ({ ...prev, status: "starting", message: t("toast.starting") }));
      const resp = await authFetch("/v1/local-llm/start", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: mid }),
      });
      const data = await resp.json();
      // 引擎忙（别的会话正在用本地模型）：不动状态卡，只提示——切换会让那些会话断流
      if (data.code === "engine_busy") {
        showToast(String(data.message || t("toast.start_fail", { msg: "" })), "warn");
        return;
      }
      setLocalLLMStatus(data);
      if (data.status === "running") {
        showToast(t("toast.started", { model: data.model_name }));
        setSelectedModel(mid);
      } else showToast(t("toast.start_fail", { msg: data.message }));
    } catch (e) { console.error(e); showToast(t("toast.conn_fail")); }
  };

  const stopLocalLLM = async () => {
    try {
      const resp = await authFetch("/v1/local-llm/stop", { method: "POST" });
      const data = await resp.json();
      // 引擎忙：后端拒绝停止（有回合正在用），照实提示，别谎报"已停止"
      if (data.code === "engine_busy") {
        showToast(String(data.message || ""), "warn");
        return;
      }
      setLocalLLMStatus(data);
      // Restore the default UI after unloading: clear the model-id input and
      // drop the local model selection in chat so the next message routes
      // through auto-routing/cloud instead of a stopped local server.
      if (data.status !== "running") {
        setLocalModelId("");
        setSelectedModel("");
      }
      showToast(t("toast.stopped"));
    } catch (e) { console.error(e) }
  };



  /* ── Session Management ── */

  return {
    localModelId, setLocalModelId, setupCheck, fixing, runFix,
    hfSearch, setHfSearch, hfResults, searching, searchHF,
    downloadProgress, downloadModel, pauseDownload, resumeDownload, cancelDownload,
    contextLimit, contextEstimate, fetchContextEstimate, updateContextLimit,
    localLLMStatus, setLocalLLMStatus, startLocalLLM, stopLocalLLM,
  };
}
