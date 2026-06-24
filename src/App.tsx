import { useState, useRef, useEffect, useCallback } from "react";
import { fetch } from "@tauri-apps/plugin-http";
import { invoke } from "@tauri-apps/api/core";
import type { Message, PendingFile, SessionInfo, ViewId, CloudModel, DownloadState, HFModelResult, LLMStatus } from "./types";
// API keys stored in OS keychain via Rust commands (store_secret/get_secret/delete_secret)
import { useSessions } from "./hooks/useSessions";
import { sidecarFetch, waitForSidecar } from "./utils/api";
import { useTranslation } from "./i18n";
import { useCronJobs } from "./hooks/useCronJobs";
import { useSkills } from "./hooks/useSkills";
import ChatView from "./components/ChatView";
import ModelsView from "./components/ModelsView";
import ToolsView from "./components/ToolsView";
import SkillsView from "./components/SkillsView";
import CronView from "./components/CronView";
import ChannelsView from "./components/ChannelsView";
import AgentView from "./components/AgentView";
import SettingsView from "./components/SettingsView";
import LogsView from "./components/LogsView";
import "./App.css";

/* ═══════════ Constants ═══════════ */

const LANG_PROMPTS: Record<string, string> = {
  zh: "请始终用简体中文回复用户，无论用户消息使用哪种语言。",
  en: "Always respond in English regardless of the language of the user's message.",
  ja: "ユーザーのメッセージの言語に関わらず、必ず日本語で回答してください。",
  ru: "Всегда отвечайте на русском языке независимо от языка сообщения пользователя.",
};

const PLAN_MODE_PROMPT =
  "【Structured Workflow — 阶段门控】\n" +
  "你必须按以下阶段顺序执行，不得跳步。\n\n" +
  "阶段1·理解：复述你对需求的理解。如有歧义或模糊之处，先提问澄清。\n\n" +
  "阶段2·方案：说明你打算怎么做——涉及哪些文件、使用哪些工具、为什么选这个方案。在获得用户确认之前不要动手。\n\n" +
  "阶段3·执行：逐步实施，每完成一步验证一步。write_file 后用 read_file 确认内容一致。run_cmd 后检查退出码。\n\n" +
  "阶段4·交付：自我审查。列出所有变更：\n" +
  "- 修改了哪些文件（完整路径）\n" +
  "- 每项验证结果（回读是否一致？命令是否成功？）\n" +
  "- 遗留问题、未完成项、后续建议\n\n" +
  "关键规则：阶段1和阶段2完成之前，不得调用任何 confirm 级别工具（write_file、run_cmd、open_app、open_folder）。";

const SIDECAR = "http://127.0.0.1:8765";

const AGENT_NAME_KEYS: Record<string, string> = {
  latiao: "agent.latiao", "code-reviewer": "agent.code_reviewer",
  "doc-generator": "agent.doc_generator", debugger: "agent.debugger",
  translator: "agent.translator",
};

const NAV_ITEMS: { id: ViewId; icon: string; key: string }[] = [
  { id: "chat", icon: "💬", key: "nav.chat" },
  { id: "models", icon: "🧠", key: "nav.models" },
  { id: "tools", icon: "🔧", key: "nav.tools" },
  { id: "skills", icon: "🧩", key: "nav.skills" },
  { id: "cron", icon: "⏰", key: "nav.cron" },
  { id: "channels", icon: "🔗", key: "nav.channels" },
  { id: "agents", icon: "🎭", key: "nav.agents" },
  { id: "settings", icon: "⚙", key: "nav.settings" },
  { id: "logs", icon: "📋", key: "nav.logs" },
];

function buildApiMessages(session: SessionInfo, extraUser?: Message, planMode?: boolean, lang?: string): Record<string, unknown>[] {
  const msgs: Record<string, unknown>[] = [];
  const langPrompt = lang ? LANG_PROMPTS[lang] : undefined;
  if (langPrompt) msgs.push({ role: "system", content: langPrompt });
  if (planMode) msgs.push({ role: "system", content: PLAN_MODE_PROMPT });
  const allMsgs = extraUser ? [...session.messages, extraUser] : session.messages;
  // Truncate long history: keep system messages + last 30 user/assistant pairs
  // Prevents context overflow for local models that struggle with long histories
  const MAX_CONTEXT_MSGS = 30;
  const recentMsgs = allMsgs.length > MAX_CONTEXT_MSGS
    ? allMsgs.slice(-MAX_CONTEXT_MSGS)
    : allMsgs;
  for (const msg of recentMsgs) {
    if (msg.role === "tool" || msg.type === "tool_call") continue;
    if (msg.role === "user" && msg.imageBase64) {
      msgs.push({
        role: "user",
        content: [
          { type: "text", text: msg.content },
          { type: "image_url", image_url: { url: `data:${msg.imageMime || "image/png"};base64,${msg.imageBase64}`, detail: "auto" } },
        ],
      });
    } else {
      msgs.push({ role: msg.role, content: msg.content });
    }
  }
  return msgs;
}

/* ═══════════ App ═══════════ */

function App() {
  /* ── Session State ── */
  const {
    sessions, setSessions, currentIdx, setCurrentIdx,
    session, messages, setSelectedModel, setMessages, newSession,
  } = useSessions();
  const { t, lang } = useTranslation();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
const [timeFilter, setTimeFilter] = useState("all");
  const [activeView, setActiveView] = useState<ViewId>("chat");
  const [modelTab, setModelTab] = useState<"cloud" | "local">("cloud");

  const [planMode, setPlanMode] = useState<boolean>(() => {
    try { const saved = localStorage.getItem("local_ai_os_plan_mode"); return saved ? JSON.parse(saved) : false; }
    catch (e) { console.error(e); return false; }
  });

  const [prompt, setPrompt] = useState("");
  const [isProcessing, setIsProcessing] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const [pendingFile, setPendingFile] = useState<PendingFile | null>(null);
  const [isRecording, setIsRecording] = useState(false);
  const [cloudModels, setCloudModels] = useState<CloudModel[]>([]);
  const [cloudModelsLoaded, setCloudModelsLoaded] = useState(false);

  // Load cloud models from OS keychain
  useEffect(() => {
    (async () => {
      try {
        const fromKeychain = await invoke("get_secret", { key: "cloud_models" }).catch(() => null) as string | null;
        if (fromKeychain) {
          setCloudModels(JSON.parse(fromKeychain));
        }
      } catch { /* ignore */ }
      setCloudModelsLoaded(true);
    })();
  }, []);
  const [newCloudModel, setNewCloudModel] = useState<CloudModel>({ name: "", key: "", endpoint: "", protocol: "openai", max_tokens: 32768 });
  const [showAdvanced, setShowAdvanced] = useState(false);


  const [sidecarStatus, setSidecarStatus] = useState<"checking" | "online" | "offline">("checking");
  const [restartingSidecar, setRestartingSidecar] = useState(false);
  const [testingModel, setTestingModel] = useState<string | null>(null);
  const [testResult, setTestResult] = useState("");
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    try { return (localStorage.getItem("latiao_theme") as "light" | "dark") || "dark"; }
    catch (e) { console.error(e); return "dark"; }
  });
  const [toast, setToast] = useState<string | null>(null);
  const [toastType, setToastType] = useState<string>("info");
  const [gatewayLogsOpen, setGatewayLogsOpen] = useState(false);
  const [gatewayLogs, setGatewayLogs] = useState<{ time: string; level: string; message: string }[]>([]);
  const [autoLaunch, setAutoLaunch] = useState(() => localStorage.getItem("latiao_auto_launch") === "true");
  const [autoStartGateway, setAutoStartGateway] = useState(() => localStorage.getItem("latiao_auto_gateway") !== "false");
  const [anonymousData, setAnonymousData] = useState(() => localStorage.getItem("latiao_anonymous_data") !== "false");
  const [autoCheckUpdate, setAutoCheckUpdate] = useState(() => localStorage.getItem("latiao_auto_check_update") !== "false");
  const [recentLearnings, setRecentLearnings] = useState<{topic: string; content: string; confidence: number}[]>([]);
  const [agentPhase, setAgentPhase] = useState<string>("");
  const [activeAgent, setActiveAgent] = useState<string>("latiao");
  const [tools, setTools] = useState<{name: string; description: string; parameters: Record<string, unknown>; permission: string; usage_count: number}[]>([]);
  const [localLLMStatus, setLocalLLMStatus] = useState<LLMStatus>({ backend: "", status: "checking", model_id: "", model_name: "", port: 1235, message: "", has_image_support: false, token_limit: 32768 });

  // Sync theme to document.documentElement so CSS variables cascade correctly
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("latiao_theme", theme);
  }, [theme]);

  const chatEndRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  /* ── Persistence: debounce during SSE streaming, immediate otherwise ── */
  const isProcessingRef = useRef(isProcessing);
  isProcessingRef.current = isProcessing;

  // Strip large fields before persisting to avoid localStorage bloat
  const stripForStorage = useCallback((s: SessionInfo[]) => {
    return s.map(session => ({
      ...session,
      messages: session.messages.slice(-200).map(m => ({
        ...m,
        imageBase64: undefined,       // don't persist base64 images
        toolResult: m.toolResult ? m.toolResult.slice(0, 5000) : undefined,
      })),
    }));
  }, []);

  // Save sessions to localStorage with quota-exceeded fallback
  const saveSessions = useCallback((data: string) => {
    try {
      localStorage.setItem("local_ai_os_sessions", data);
    } catch {
      // Quota exceeded — prune oldest sessions and retry
      try {
        const current = JSON.parse(localStorage.getItem("local_ai_os_sessions") || "[]");
        if (Array.isArray(current) && current.length > 1) {
          localStorage.setItem("local_ai_os_sessions", JSON.stringify(current.slice(-2)));
        } else {
          localStorage.removeItem("local_ai_os_sessions");
        }
        localStorage.setItem("local_ai_os_sessions", data);
      } catch {
        // Still failing — data will be lost for this session
      }
    }
  }, []);

  useEffect(() => {
    const stripped = stripForStorage(sessions);
    const data = JSON.stringify(stripped);
    if (isProcessing) {
      // Streaming: debounce to 1s to avoid thrashing
      const timer = setTimeout(() => saveSessions(data), 1000);
      return () => clearTimeout(timer);
    } else {
      // Not streaming: save immediately
      saveSessions(data);
    }
  }, [sessions, isProcessing, stripForStorage, saveSessions]);

  // Auto-scroll chat to bottom (throttled to avoid jank during SSE streaming)
  useEffect(() => {
    const timer = setTimeout(() => {
      chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }, 150);
    return () => clearTimeout(timer);
  }, [messages]);
  useEffect(() => { localStorage.setItem("local_ai_os_plan_mode", JSON.stringify(planMode)); }, [planMode]);
  // Persist cloud models to OS keychain (debounced to avoid writes on every keystroke)
  useEffect(() => {
    if (!cloudModelsLoaded) return;
    const timer = setTimeout(async () => {
      try {
        await invoke("store_secret", { key: "cloud_models", value: JSON.stringify(cloudModels) });
      } catch { /* ignore */ }
    }, 1000);
    return () => clearTimeout(timer);
  }, [cloudModels, cloudModelsLoaded]);
  // Intercept external links → open in system browser (via tauri-plugin-opener)
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const a = (e.target as HTMLElement).closest("a");
      if (!a || !a.href) return;
      try {
        const url = new URL(a.href);
        if (url.protocol !== "http:" && url.protocol !== "https:") return;
        if (["127.0.0.1", "localhost", "tauri.localhost"].includes(url.hostname)) return;
        e.preventDefault();
        invoke("plugin:opener|open_url", { url: a.href });
      } catch {}
    };
    document.addEventListener("click", handler, true);
    return () => document.removeEventListener("click", handler, true);
  }, []);


  const [fetchDiag, setFetchDiag] = useState("🔍 正在获取...");


  // Fetch tools from sidecar (via Rust IPC proxy) with health check + retry
  const fetchTools = async () => {
    setFetchDiag("🔍 检查 Sidecar 状态...");
    const healthy = await waitForSidecar();
    if (!healthy) {
      setFetchDiag("❌ Sidecar 无响应，请确认 http://127.0.0.1:8765 已启动");
      return;
    }

    const maxRetries = 5;
    for (let attempt = 0; attempt < maxRetries; attempt++) {
      try {
        if (attempt > 0) {
          setFetchDiag(`⏳ 重试获取工具列表... (${attempt}/${maxRetries})`);
          await new Promise(r => setTimeout(r, 2000));
        } else {
          setFetchDiag("⏳ 正在获取工具列表...");
        }
        const data = await sidecarFetch("/v1/tools");
        setFetchDiag(`✅ /v1/tools → status=${data.status}`);
        if (data.status === "ok") {
          setTools(data.tools || []);
          setFetchDiag(d => `${d}, tools=${data.tools?.length || 0}`);
        }
        return; // success
      } catch (e: any) {
        if (attempt === maxRetries - 1) {
          setFetchDiag(`❌ 错误: ${e?.message || String(e)} (已重试${maxRetries}次)`);
        }
      }
    }
  };
  useEffect(() => { fetchTools(); }, []);

  // Restart sidecar
  const handleRestartSidecar = async () => {
    setRestartingSidecar(true);
    setSidecarStatus("checking");
    try {
      await invoke("restart_sidecar");
      // Wait for sidecar to come back online (up to 15s)
      for (let i = 0; i < 15; i++) {
        await new Promise(r => setTimeout(r, 1000));
        try {
          const resp = await fetch(SIDECAR + "/health", { signal: AbortSignal.timeout(2000) });
          if (resp.ok) {
            setSidecarStatus("online");
            showToast("Sidecar 已重启");
            return;
          }
        } catch { /* still starting */ }
      }
      showToast("Sidecar 重启后无响应，请检查");
    } catch (e: any) {
      showToast(`重启失败: ${e?.message || String(e)}`);
    } finally {
      setRestartingSidecar(false);
    }
  };

  // Unified heartbeat: sidecar status + downloads + learnings
  useEffect(() => {
    const tick = async () => {
      // Unified sidecar heartbeat
      try {
        const resp = await fetch(SIDECAR + "/v1/heartbeat");
        const data = await resp.json();
        if (data.status === "ok") {
          setSidecarStatus("online");
          setLocalLLMStatus(data.local_llm);
          // Downloads
          const map: Record<string, DownloadState> = {};
          (data.downloads || []).forEach((d: DownloadState) => { map[d.model_id || ""] = d; });
          setDownloadProgress(map);
          // Learnings
          setRecentLearnings(data.learnings || []);
        } else {
          setSidecarStatus("offline");
        }
      } catch { setSidecarStatus("offline"); }

      // Fetch recent logs (always, cheap ring-buffer read)
      try {
        const lr = await fetch(SIDECAR + "/v1/logs?limit=100");
        const ld = await lr.json();
        if (ld.status === "ok") setGatewayLogs(ld.logs || []);
      } catch { /* ignore */ }
    };
    tick();
    const interval = setInterval(tick, 5000);
    return () => clearInterval(interval);
  }, []);

  const [localModelId, setLocalModelId] = useState("");
  const [setupCheck, setSetupCheck] = useState<{ready: boolean; ok: {item: string; status: string}[]; issues: {item: string; status: string; fix: string}[]} | null>(null);
  const [hfSearch, setHfSearch] = useState("");
  const [hfResults, setHfResults] = useState<HFModelResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [downloadProgress, setDownloadProgress] = useState<Record<string, DownloadState>>({});
  const [fixing, setFixing] = useState("");
  const [contextLimit, setContextLimit] = useState(8192);
  const [contextEstimate, setContextEstimate] = useState<{max_context: number; recommended_context: number; ram_available_gb: number; memory_for_context_gb: number} | null>(null);

  // Fetch context estimate
  const fetchContextEstimate = async (modelPath?: string) => {
    try {
      const params = modelPath ? `?model_path=${encodeURIComponent(modelPath)}` : "";
      const resp = await fetch(SIDECAR + "/v1/local-llm/estimate-context" + params);
      const data = await resp.json();
      if (data.max_context) setContextEstimate(data);
      if (data.current_context) setContextLimit(data.current_context);
    } catch { /* ignore */ }
  };
  useEffect(() => { fetchContextEstimate(); }, []);

  // Set context limit
  const updateContextLimit = async (limit: number) => {
    setContextLimit(limit);
    try {
      await fetch(SIDECAR + "/v1/local-llm/context-limit", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ limit }),
      });
    } catch { /* ignore */ }
  };

  // Fetch setup check on mount
  const fetchSetup = () => {
    fetch(SIDECAR + "/v1/local-llm/setup").then(r => r.json()).then(d => setSetupCheck(d)).catch(() => {});
  };
  useEffect(() => { fetchSetup(); }, []);

  const runFix = async (fixType: string, fixPkg: string) => {
    setFixing(fixPkg);
    try {
      const resp = await fetch(SIDECAR + "/v1/local-llm/fix", {
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
  useEffect(() => {
    const poll = async () => {
      try {
        const resp = await fetch(SIDECAR + "/v1/local-llm/downloads");
        const data = await resp.json();
        if (data.status === "ok" && Array.isArray(data.downloads)) {
          const progress: Record<string, DownloadState> = {};
          for (const dl of data.downloads) {
            progress[dl.model_id] = dl;
          }
          setDownloadProgress(progress);
        }
      } catch { /* ignore poll errors */ }
    };
    poll();
    const interval = setInterval(poll, 2000);
    return () => clearInterval(interval);
  }, []);

  const searchHF = useCallback(async (query?: string, library?: string) => {
    const q = query ?? hfSearch;
    setSearching(true);
    try {
      const libParam = library ? `&library=${encodeURIComponent(library)}` : "";
      const resp = await fetch(`${SIDECAR}/v1/local-llm/search?q=${encodeURIComponent(q)}&limit=30${libParam}`);
      const data = await resp.json();
      if (data.status === "ok") setHfResults(data.results);
    } catch (e) { console.error(e) }
    setSearching(false);
  }, [hfSearch]);

  // Auto-search with debounce as user types
  useEffect(() => {
    if (!hfSearch.trim()) { setHfResults([]); return; }
    const timer = setTimeout(() => searchHF(hfSearch), 400);
    return () => clearTimeout(timer);
  }, [hfSearch, searchHF]);

  const downloadModel = async (modelId: string) => {
    showToast(t("toast.dl_start", { name: modelId.split("/").pop() || modelId }));
    try {
      const resp = await fetch(SIDECAR + "/v1/local-llm/download", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: modelId }),
      });
      const data = await resp.json();
      if (data.status === "ok") {
        // Immediately fetch downloads to show UI feedback
        const dlResp = await fetch(SIDECAR + "/v1/local-llm/downloads");
        const dlData = await dlResp.json();
        if (dlData.status === "ok" && Array.isArray(dlData.downloads)) {
          const progress: Record<string, DownloadState> = {};
          for (const dl of dlData.downloads) {
            if (dl.model_id) progress[dl.model_id] = dl;
          }
          setDownloadProgress(prev => ({ ...prev, ...progress }));
        }
      } else {
        showToast(t("toast.dl_fail") + ": " + (data.message || ""));
      }
    } catch (e) { console.error(e); showToast(t("toast.dl_fail")); }
  };

  const pauseDownload = async (modelId: string) => {
    await fetch(SIDECAR + "/v1/local-llm/download/pause", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_id: modelId }),
    });
  };

  const resumeDownload = async (modelId: string) => {
    await fetch(SIDECAR + "/v1/local-llm/download/resume", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_id: modelId }),
    });
  };

  const cancelDownload = async (modelId: string) => {
    await fetch(SIDECAR + "/v1/local-llm/download/cancel", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_id: modelId }),
    });
  };

  const startLocalLLM = async (modelId?: string) => {
    const mid = (modelId || localModelId).trim();
    if (!mid) { showToast(t("toast.need_model_id")); return; }
    if (modelId) setLocalModelId(modelId);
    try {
      setLocalLLMStatus(prev => ({ ...prev, status: "starting", message: t("toast.starting") }));
      const resp = await fetch(SIDECAR + "/v1/local-llm/start", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: mid }),
      });
      const data = await resp.json();
      setLocalLLMStatus(data);
      if (data.status === "running") {
        showToast(t("toast.started", { model: data.model_name }));
        setSelectedModel(mid);
      } else showToast(t("toast.start_fail", { msg: data.message }));
    } catch (e) { console.error(e); showToast(t("toast.conn_fail")); }
  };

  const stopLocalLLM = async () => {
    try {
      const resp = await fetch(SIDECAR + "/v1/local-llm/stop", { method: "POST" });
      const data = await resp.json();
      setLocalLLMStatus(data);
      showToast(t("toast.stopped"));
    } catch (e) { console.error(e) }
  };



  /* ── Session Management ── */
  const switchSession = (idx: number) => { setCurrentIdx(idx); setPendingFile(null); setActiveView("chat"); };
  const deleteSession = (idx: number) => {
    const makeNew = () => ({ id: `session_${Math.random().toString(36).substring(7)}`, name: "session.default", messages: [] as Message[], selectedModel: "" });
    setSessions((prev) => {
      const next = prev.filter((_, i) => i !== idx);
      if (next.length === 0) return [makeNew()];
      return next;
    });
    if (currentIdx >= idx) setCurrentIdx((c) => Math.max(0, c - 1));
  };
  /* ── Toast ── */
  const showToast = useCallback((msg: string, type?: string) => {
    setToast(msg);
    setToastType(type || "info");
    setTimeout(() => setToast(null), 2200);
  }, []);

  // Extracted hooks (cron + skills — depend on showToast)
  const { cronJobs, newCron, setNewCron, addCronJob, toggleCronJob, deleteCronJob } = useCronJobs(showToast);
  const { skills, newSkill, setNewSkill, toggleSkill, deleteSkill, addSkill, tavilyKey, saveTavilyKey, deleteTavilyKey } = useSkills(showToast);

  /* ── Stream Chat (preserved from original) ── */
  const streamChat = async (
    messages: Record<string, unknown>[],
    opts?: { model?: string; agent?: string; cloudConfig?: Record<string, unknown>; skipTools?: boolean },
    signal?: AbortSignal,
  ): Promise<string> => {
    const body: Record<string, unknown> = { messages, stream: true };
    if (opts?.model) body.model = opts.model;
    if (opts?.agent) body.agent = opts.agent;
    if (opts?.cloudConfig) body.cloud_config = opts.cloudConfig;
    if (opts?.skipTools) body.skip_tools = true;

    let response: Response;
    try {
      response = await fetch(SIDECAR + "/v1/chat/completions", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal,
      });
    } catch (e) {
      throw new Error(`无法连接 Sidecar\n原始错误: ${e}`);
    }
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "", full = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (line.startsWith("data: ")) {
          const data = line.substring(6).trim();
          if (data === "[DONE]") return full;
          try {
            const parsed = JSON.parse(data);
            try {
              if (parsed.error) throw new Error(parsed.error);
              if (parsed.event === "tool_confirm") {
                setAgentPhase(t("agent.phase_confirm", { tool: parsed.tool || "" }));
                setMessages((prev) => {
                  const msgs = [...prev];
                  msgs.splice(msgs.length - 1, 0, {
                    role: "tool", type: "tool_call", content: "",
                    callId: parsed.call_id, toolName: parsed.tool, toolArgs: parsed.args, toolStatus: "confirming",
                  });
                  return msgs;
                });
              } else if (parsed.event === "tool_start") {
                setMessages((prev) => {
                  const msgs = [...prev];
                  const idx = msgs.findIndex((m) => m.callId === parsed.call_id && m.toolStatus === "confirming");
                  if (idx !== -1) { msgs[idx] = { ...msgs[idx], toolStatus: "running" }; }
                  else {
                    msgs.splice(msgs.length - 1, 0, {
                      role: "tool", type: "tool_call", content: "",
                      callId: parsed.call_id, toolName: parsed.tool, toolArgs: parsed.args, toolStatus: "running",
                    });
                  }
                  return msgs;
                });
              } else if (parsed.event === "tool_end") {
                const rawResult = String(parsed.result ?? "");
                const toolResult = rawResult.length > 10000
                  ? rawResult.slice(0, 10000) + `\n\n...(截断)`
                  : rawResult;
                const isError = rawResult.startsWith("Error") || rawResult.startsWith("⛔");
                setMessages((prev) => {
                  const msgs = [...prev];
                  const idx = msgs.findIndex((m) => m.callId === parsed.call_id && (m.toolStatus === "running" || m.toolStatus === "confirming"));
                  if (idx !== -1) {
                    msgs[idx] = { ...msgs[idx], toolResult, toolStatus: isError ? "error" : "done", content: toolResult };
                  }
                  return msgs;
                });
              } else if (parsed.content) {
                full += parsed.content;
                setMessages((prev) => {
                  const msgs = [...prev];
                  const last = msgs[msgs.length - 1];
                  if (last?.role === "assistant") msgs[msgs.length - 1] = { ...last, content: full };
                  return msgs;
                });
              }
            } catch { /* skip malformed event */ }
          } catch (e) { if (e instanceof SyntaxError) continue; throw e; }
        }
      }
    }
    return full;
  };

  const confirmTool = useCallback(async (callId: string, approved: boolean) => {
    try {
      const resp = await fetch(SIDECAR + "/v1/confirm_tool", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ call_id: callId, approved }),
      });
      const data = await resp.json();
      if (data.status === "not_found") {
        showToast(t("toast.timeout"));
        setMessages(prev => prev.map(m => m.callId === callId && m.toolStatus === "confirming" ? { ...m, toolStatus: "error" as const, toolResult: t("toast.timeout_detail") } : m));
      }
    } catch (e) { console.error(e) }
  }, [showToast, setMessages, t]);

  const stopGeneration = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setIsProcessing(false);
    setPendingFile(null);
    setAgentPhase("");
  }, []);

  /* ── Send Message ── */
  const sendMessage = async () => {
    const text = prompt;
    if (!text.trim() && !pendingFile) return;
    setPrompt("");
    setIsProcessing(true);
    setAgentPhase(t("agent.phase_analyze"));

    const userMsg: Message = { role: "user", content: text || "Analyze this file" };
    if (pendingFile) {
      userMsg.type = pendingFile.type === "image" ? "image" : "file";
      userMsg.filename = pendingFile.name;
      if (pendingFile.base64) { userMsg.imageBase64 = pendingFile.base64; userMsg.imageMime = pendingFile.mimeType; }
      userMsg.content = text || `[File: ${pendingFile.name}]`;
    }

    setMessages((prev) => [...prev, userMsg]);

    const assistantPlaceholder: Message = { role: "assistant", content: "" };
    setMessages((prev) => [...prev, assistantPlaceholder]);

    try {
      const apiMessages = buildApiMessages(session, userMsg, planMode, lang);
      const cloudCfg = session.selectedModel
        ? cloudModels.find((m) => m.name === session.selectedModel)
        : undefined;
      const opts: Record<string, unknown> = { agent: activeAgent };
      if (session.selectedModel) opts.model = session.selectedModel;
      if (cloudCfg) opts.cloudConfig = { key: cloudCfg.key, endpoint: cloudCfg.endpoint, protocol: cloudCfg.protocol || "openai" };

      const controller = new AbortController();
      abortControllerRef.current = controller;
      await streamChat(apiMessages, opts, controller.signal);
    } catch (e) {
      setMessages((prev) => {
        const msgs = [...prev];
        const last = msgs[msgs.length - 1];
        if (last?.role === "assistant") msgs[msgs.length - 1] = { ...last, content: `❌ ${e}` };
        return msgs;
      });
    } finally {
      abortControllerRef.current = null;
      setIsProcessing(false);
      setPendingFile(null);
      setAgentPhase("");
    }
  };

  /* ── File Upload ── */
  // Resize image to max 1024px longest side (reduces token cost)
  const resizeImage = (file: File): Promise<{ base64: string; mime: string; preview: string }> => {
    return new Promise((resolve, reject) => {
      const img = new Image();
      const objectUrl = URL.createObjectURL(file);
      img.onload = () => {
        URL.revokeObjectURL(objectUrl);
        const MAX = 1024;
        let { width, height } = img;
        if (width > MAX || height > MAX) {
          const ratio = Math.min(MAX / width, MAX / height);
          width = Math.round(width * ratio);
          height = Math.round(height * ratio);
        }
        const canvas = document.createElement("canvas");
        canvas.width = width; canvas.height = height;
        const ctx = canvas.getContext("2d")!;
        ctx.drawImage(img, 0, 0, width, height);
        const mime = "image/jpeg";
        const dataUrl = canvas.toDataURL(mime, 0.85);
        resolve({ base64: dataUrl.split(",")[1], mime, preview: dataUrl });
      };
      img.onerror = () => {
        URL.revokeObjectURL(objectUrl);
        reject(new Error("Failed to load image"));
      };
      img.src = objectUrl;
    });
  };

  const processImageFile = async (file: File, name?: string) => {
    if (file.type.startsWith("image/")) {
      const { base64, mime, preview } = await resizeImage(file);
      setPendingFile({ name: name || file.name, preview, type: "image", content: preview, base64, mimeType: mime });
    }
  };

  const handleDrop = useCallback(async (e: React.DragEvent) => {
    e.preventDefault();
    const file = e.dataTransfer?.files?.[0];
    if (file?.type.startsWith("image/")) {
      await processImageFile(file);
    }
  }, []);

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.type.startsWith("image/")) {
      await processImageFile(file);
    } else {
      const reader = new FileReader();
      reader.onload = () => {
        const result = reader.result as string;
        if (file.type === "application/pdf") {
          setPendingFile({ name: file.name, preview: "📄", type: "pdf", content: result, base64: result.split(",")[1], mimeType: file.type });
        } else {
          setPendingFile({ name: file.name, preview: "📄", type: "file", content: result });
        }
      };
      if (file.type === "application/pdf") {
        reader.readAsDataURL(file);
      } else {
        reader.readAsText(file);
      }
    }
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  /* ── Speech Recognition ── */
  const startRecording = async () => {
    try {
      const nav = window.navigator || navigator;
      if (!nav.mediaDevices?.getUserMedia) {
        showToast(t("toast.no_mic"));
        return;
      }
      const stream = await nav.mediaDevices.getUserMedia({ audio: true });
      const mediaRecorder = new MediaRecorder(stream, { mimeType: "audio/webm" });
      mediaRecorderRef.current = mediaRecorder;
      audioChunksRef.current = [];
      mediaRecorder.ondataavailable = (event) => { if (event.data.size > 0) audioChunksRef.current.push(event.data); };
      mediaRecorder.onstop = async () => {
        setIsRecording(false);
        const audioBlob = new Blob(audioChunksRef.current, { type: "audio/webm" });
        const arrayBuffer = await audioBlob.arrayBuffer();
        const uint8Array = new Uint8Array(arrayBuffer);
        let binary = "";
        for (let i = 0; i < uint8Array.byteLength; i++) binary += String.fromCharCode(uint8Array[i]);
        const base64 = btoa(binary);

        try {
          const resp = await fetch(SIDECAR + "/v1/recognize_speech", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ audio_base64: base64, mime_type: "audio/webm" }),
          });
          const data = await resp.json();
          if (data.text) {
            setPrompt(data.text);
          }
        } catch (e) { console.error(e); showToast(t("toast.speech_fail")); }
        stream.getTracks().forEach((t) => t.stop());
      };
      mediaRecorder.start();
      setIsRecording(true);
    } catch {
      setIsRecording(false);
      showToast(t("toast.mic_denied"));
    }
  };

  /* ── API Test ── */
  const testConnection = async (modelName: string, key: string, endpoint: string, protocol: string) => {
    setTestingModel(modelName);
    setTestResult("");
    try {
      const resp = await fetch(SIDECAR + "/v1/test_connection", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: modelName, key, endpoint, protocol }),
      });
      const data = await resp.json();
      setTestResult(data.status === "ok" ? "✅ 连接成功" : `❌ ${data.message || "连接失败"}`);
    } catch { setTestResult("❌ 无法连接 Sidecar"); }
    finally { setTestingModel(null); }
  };


  /* ═══════════ Render ═══════════ */

  return (
    <div className={`app${sidebarCollapsed ? " sidebar-collapsed" : ""}`} data-theme={theme}>

      {/* ═══ Sidebar ═══ */}
      <aside className="sidebar">
        <div className="sidebar-brand">
          {!sidebarCollapsed && <div className="sidebar-logo">辣</div>}
          {!sidebarCollapsed && <div>
            <div className="sidebar-title">辣条</div>
            <div className="sidebar-subtitle">Latiao</div>
          </div>}
          <button className="sidebar-collapse-btn" onClick={() => setSidebarCollapsed(!sidebarCollapsed)} title="折叠侧边栏">{sidebarCollapsed ? "☰" : "◁"}</button>
        </div>

        <nav className="sidebar-nav">
          <div className="nav-section-label" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            {!sidebarCollapsed && <span>{t("sidebar.sessions")}</span>}
            <button className="session-add-btn" onClick={() => {
              const ns = newSession();
              setSessions((prev) => [...prev, ns]);
              setCurrentIdx(sessions.length);
              setPendingFile(null);
              setActiveView("chat");
            }} title={t("sidebar.new")}>+</button>
          </div>
          {!sidebarCollapsed && (() => {
              const today = new Date(); today.setHours(0,0,0,0);
              const weekStart = new Date(today); weekStart.setDate(today.getDate() - today.getDay());
              const monthStart = new Date(today.getFullYear(), today.getMonth(), 1);
              const t1 = today.getTime(), w1 = weekStart.getTime(), m1 = monthStart.getTime();
              const filtered = sessions.filter(s => {
                const la = s.lastActive || 0;
                if (timeFilter === "today") return la >= t1;
                if (timeFilter === "week") return la >= w1;
                if (timeFilter === "month") return la >= m1;
                if (timeFilter === "older") return la < m1;
                return true;
              });
              return (<>
                <div className="time-filter" style={{display:"flex",gap:4,padding:"4px 0",flexWrap:"wrap"}}>
                  {["all","today","week","month","older"].map(f => (
                    <button key={f} className={`btn btn-sm ${timeFilter===f?"btn-primary":"btn-ghost"}`}
                      style={{fontSize:10,padding:"2px 6px"}}
                      onClick={() => setTimeFilter(f)}>{t("sidebar.filter_"+f)}</button>
                  ))}
                </div>
                {filtered.map((s) => {const idx = sessions.indexOf(s); return (
          
            <button key={s.id} className={`session-item${idx === currentIdx ? " active" : ""}`} onClick={() => switchSession(idx)}>
              <span className="session-info">
                <div className="session-name">{s.name.startsWith("session.") ? t(s.name) : s.name}</div>
                <div className="session-preview">{s.messages.length > 0 ? (s.messages[s.messages.length - 1].content || "").slice(0, 30) + "..." : t("session.default")}</div>
              </span>
              <span className="session-delete-btn" style={idx === currentIdx ? { opacity: 1 } : undefined}
                onClick={(e) => { e.stopPropagation(); deleteSession(idx); }}>×</span>
            </button>
          );})}
          </>)})()}
          
          <div className="nav-section-label">{t("sidebar.nav")}</div>
          {NAV_ITEMS.map((item) => (
            <button key={item.id} className={`nav-item${activeView === item.id ? " active" : ""}`}
              onClick={() => setActiveView(item.id)}>
              <span className="nav-icon">{item.icon}</span> {!sidebarCollapsed && t(item.key)}
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          <select className="sidebar-model-select" value={session.selectedModel} onChange={(e) => { setSelectedModel(e.target.value); showToast(t("toast.model_switched", { model: e.target.value || t("sidebar.auto_detect") })); }}>
            <option value="">{t("sidebar.auto_detect")}</option>
            {cloudModels.map((m) => (<option key={m.name} value={m.name}>☁️ {m.name}</option>))}
          </select>
          <button className="sidebar-settings-btn" title={t("nav.settings")} onClick={() => setActiveView("settings")}>⚙</button>
        </div>
      </aside>

      {/* ═══ Main ═══ */}
      <main className="main">
        <div className="topbar">
          <span className="topbar-title">{session.name}</span>
          <span style={{ fontSize: 10, color: "var(--text-muted)", padding: "2px 6px", borderRadius: "var(--radius-sm)", background: "var(--accent-soft)", marginRight: 8 }}>
            {t(AGENT_NAME_KEYS[activeAgent] || activeAgent)}
          </span>
          <span className={`status-dot ${sidecarStatus === "online" ? "online" : "offline"}`}></span>
          <span className="status-label">{sidecarStatus === "online" ? t("sidebar.online") : t("sidebar.offline")}</span>
          {isProcessing && agentPhase && (
            <span style={{ fontSize: 11, color: "var(--accent)", marginLeft: 8, padding: "2px 8px", borderRadius: "var(--radius-sm)", background: "var(--accent-soft)" }}>
              {agentPhase}
            </span>
          )}
          {isProcessing && (
            <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: 4 }}>{t("sidebar.processing")}</span>
          )}
        </div>


        {/* ═══ Chat View ═══ */}
        <div className={`view-panel${activeView === "chat" ? " active" : ""}`} id="view-chat">
          <ChatView
            messages={messages} isProcessing={isProcessing}
            pendingFile={pendingFile} setPendingFile={setPendingFile}
            prompt={prompt} setPrompt={setPrompt}
            planMode={planMode} setPlanMode={setPlanMode}
            fileInputRef={fileInputRef} mediaRecorderRef={mediaRecorderRef}
            isRecording={isRecording}
            sendMessage={sendMessage} onStop={stopGeneration} handleFileSelect={handleFileSelect}
            startRecording={startRecording} confirmTool={confirmTool}
            chatEndRef={chatEndRef} handleDrop={handleDrop}
            onPasteImage={(file) => processImageFile(file, `截图 ${new Date().toLocaleTimeString()}`)}
          />
        </div>

        {/* ═══ Models View ═══ */}
        <div className={`view-panel${activeView === "models" ? " active" : ""}`} id="view-models">
          <div className="page-header">
            <div><div className="page-title">{t("page.models")}</div><div className="page-desc">{t("page.models_desc", { model: session.selectedModel || t("sidebar.auto_detect") })}</div></div>
          </div>
          <div className="tab-bar">
            <button className={`tab-btn${modelTab === "cloud" ? " active" : ""}`} onClick={() => setModelTab("cloud")}>☁️ {t("page.models_cloud")}</button>
            <button className={`tab-btn${modelTab === "local" ? " active" : ""}`} onClick={() => setModelTab("local")}>🖥️ {t("page.models_local")}</button>
          </div>
          <ModelsView
            modelTab={modelTab}
            selectedModel={session.selectedModel} setSelectedModel={setSelectedModel}
            cloudModels={cloudModels} setCloudModels={setCloudModels}
            newCloudModel={newCloudModel} setNewCloudModel={setNewCloudModel}
            showAdvanced={showAdvanced} setShowAdvanced={setShowAdvanced}
            testingModel={testingModel} testResult={testResult}
            testConnection={testConnection}
            recentLearnings={recentLearnings}
            localLLMStatus={localLLMStatus}
            localModelId={localModelId} setLocalModelId={setLocalModelId}
            setupCheck={setupCheck}
            hfSearch={hfSearch} setHfSearch={setHfSearch}
            hfResults={hfResults} searching={searching} searchHF={searchHF}
            downloadProgress={downloadProgress}
            downloadModel={downloadModel} pauseDownload={pauseDownload}
            resumeDownload={resumeDownload} cancelDownload={cancelDownload}
            startLocalLLM={startLocalLLM} stopLocalLLM={stopLocalLLM}
            fixing={fixing} runFix={runFix}
            showToast={showToast}
            contextLimit={contextLimit} setContextLimit={updateContextLimit}
            contextEstimate={contextEstimate} fetchContextEstimate={fetchContextEstimate}
          />
        </div>

        {/* ═══ Tools View ═══ */}
        <div className={`view-panel${activeView === "tools" ? " active" : ""}`} id="view-tools">
          <div className="page-header">
            <div><div className="page-title">{t("page.tools")}</div><div className="page-desc">{t("page.tools_desc", { count: tools.length })}</div></div>
          </div>
          <div className="page-body">
            {tools.length === 0 && <div style={{padding:20,color:'var(--warning)',fontFamily:'monospace',whiteSpace:'pre-wrap'}}>{fetchDiag}</div>}
            <ToolsView tools={tools} setTools={setTools} showToast={showToast} />
          </div>
        </div>
        <div className={`view-panel${activeView === "skills" ? " active" : ""}`} id="view-skills">
          <div className="page-header">
            <div><div className="page-title">{t("page.skills")}</div><div className="page-desc">{t("page.skills_desc", { enabled: skills.filter(s => s.enabled).length, total: skills.length })}</div></div>
          </div>
          <div className="page-body">
            <SkillsView skills={skills} newSkill={newSkill} setNewSkill={setNewSkill}
              toggleSkill={toggleSkill} deleteSkill={deleteSkill} addSkill={addSkill}
              tavilyKey={tavilyKey} onSaveTavilyKey={saveTavilyKey} onDeleteTavilyKey={deleteTavilyKey} />
          </div>
        </div>


        {/* ═══ Cron View ═══ */}
        <div className={`view-panel${activeView === "cron" ? " active" : ""}`} id="view-cron">
          <div className="page-header">
            <div><div className="page-title">{t("page.cron")}</div><div className="page-desc">{t("page.cron_desc", { count: cronJobs.filter(j => j.enabled).length })}</div></div>
          </div>
          <div className="page-body">
            <CronView key={lang} cronJobs={cronJobs} newCron={newCron} setNewCron={setNewCron}
              toggleCronJob={toggleCronJob} deleteCronJob={deleteCronJob} addCronJob={addCronJob} />
          </div>
        </div>
        {/* ═══ Channels View ═══ */}
        <div className={`view-panel${activeView === "channels" ? " active" : ""}`} id="view-channels">
          <div className="page-header">
            <div><div className="page-title">{t("page.channels")}</div><div className="page-desc">{t("page.channels_desc")}</div></div>
          </div>
          <div className="page-body">
            <ChannelsView />
          </div>
        </div>


        {/* ═══ Agent View ═══ */}
        <div className={`view-panel${activeView === "agents" ? " active" : ""}`} id="view-agents">
          <div className="page-header">
            <div><div className="page-title">{t("page.agents")}</div><div className="page-desc">{t("page.agents_desc")}</div></div>
            <button className="btn btn-md btn-primary" style={{ marginLeft: "auto" }} onClick={() => showToast(t("agent.created_simple"))}>{t("agent.new_btn")}</button>
          </div>
          <div className="page-body">
            <AgentView key={lang} activeAgent={activeAgent} setActiveAgent={setActiveAgent} showToast={showToast} />
          </div>
        </div>
        <div className={`view-panel${activeView === "settings" ? " active" : ""}`} id="view-settings">
          <div className="page-header">
            <div><div className="page-title">{t("page.settings")}</div><div className="page-desc">{t("page.settings_desc")}</div></div>
          </div>
          <SettingsView
            theme={theme} setTheme={setTheme}
            sidecarStatus={sidecarStatus}
            restartingSidecar={restartingSidecar}
            onRestartSidecar={handleRestartSidecar}
            gatewayLogsOpen={gatewayLogsOpen} setGatewayLogsOpen={setGatewayLogsOpen}
            gatewayLogs={gatewayLogs}
            selectedModel={session.selectedModel}
            cloudModels={cloudModels}
            setActiveView={(v) => setActiveView(v as ViewId)}
            autoLaunch={autoLaunch} setAutoLaunch={setAutoLaunch}
            autoStartGateway={autoStartGateway} setAutoStartGateway={setAutoStartGateway}
            anonymousData={anonymousData} setAnonymousData={setAnonymousData}
            autoCheckUpdate={autoCheckUpdate} setAutoCheckUpdate={setAutoCheckUpdate}
          />
        </div>

        {/* ═══ Logs View ═══ */}
        <div className={`view-panel${activeView === "logs" ? " active" : ""}`} id="view-logs">
          <div className="page-header">
            <div>
              <div className="page-title">{t("page.logs")}</div>
              <div className="page-desc">{t("page.logs_desc", { count: gatewayLogs.length })}</div>
            </div>
          </div>
          <div className="page-body">
            <LogsView logs={gatewayLogs} />
          </div>
        </div>

      </main>

      {/* ═══ Toast ═══ */}
      {toast && (
        <div style={{
          position: "fixed", bottom: 24, right: 24, zIndex: 9999,
          padding: "10px 18px", borderRadius: "var(--radius-md)",
          background: "var(--bg-elevated)",
          border: `1px solid ${toastType === "warning" ? "var(--warning)" : toastType === "success" ? "var(--success)" : "var(--border-strong)"}`,
          borderLeft: `3px solid ${toastType === "warning" ? "var(--warning)" : toastType === "success" ? "var(--success)" : "var(--accent)"}`,
          color: "var(--text-primary)", fontSize: 12, fontFamily: "var(--font-sans)",
          backdropFilter: "blur(14px)", WebkitBackdropFilter: "blur(14px)",
          animation: "fadeInMsg 0.25s ease", boxShadow: "0 8px 32px rgba(0,0,0,0.4)",
        }}>
          {toast}
        </div>
      )}
    </div>
  );
}

export default App;
