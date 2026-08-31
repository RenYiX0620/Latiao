import { useState, useRef, useEffect, useCallback } from "react";
import { fetch } from "@tauri-apps/plugin-http";
import { invoke } from "@tauri-apps/api/core";
import logoUrl from "./assets/logo.png";
import type { Message, PendingFile, SessionInfo, ViewId, CloudModel, DownloadState, HFModelResult, LLMStatus } from "./types";
// API keys stored in OS keychain via Rust commands (store_secret/get_secret/delete_secret)
import { useSessions } from "./hooks/useSessions";
import { sidecarFetch, waitForSidecar, authFetch } from "./utils/api";
import { useTranslation } from "./i18n";
import { useCronJobs } from "./hooks/useCronJobs";
import ChatView from "./components/ChatView";
import ModelsView from "./components/ModelsView";
import ToolsView from "./components/ToolsView";
import CronView from "./components/CronView";
import ChannelsView from "./components/ChannelsView";
import AgentView from "./components/AgentView";
import SettingsView from "./components/SettingsView";
import RecoveryView from "./components/RecoveryView";
import LogsView from "./components/LogsView";
import { MessageSquare, Brain, Wrench, Clock, Radio, Bot, Settings, ScrollText } from "lucide-react";
import type { LucideIcon } from "lucide-react";
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

// Unique id for each chat message so list rendering can use a stable key
// (index keys break when tool messages are spliced into the middle).
const msgId = () => `msg_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;

// 统一能力模型：工具与技能合并的条目（capabilities 表）
interface Capability {
  name: string;
  kind: "tool" | "skill";
  display_name: string;
  description: string;
  permission: string;
  enabled: boolean;
  source: string;
  usage_count: number;
}

const AGENT_NAME_KEYS: Record<string, string> = {
  latiao: "agent.latiao", "code-reviewer": "agent.code_reviewer",
  "doc-generator": "agent.doc_generator", debugger: "agent.debugger",
  translator: "agent.translator",
};

const NAV_ITEMS: { id: ViewId; icon: LucideIcon; key: string }[] = [
  { id: "chat", icon: MessageSquare, key: "nav.chat" },
  { id: "models", icon: Brain, key: "nav.models" },
  { id: "tools", icon: Wrench, key: "nav.tools" },
  { id: "cron", icon: Clock, key: "nav.cron" },
  { id: "channels", icon: Radio, key: "nav.channels" },
  { id: "agents", icon: Bot, key: "nav.agents" },
  { id: "settings", icon: Settings, key: "nav.settings" },
  { id: "logs", icon: ScrollText, key: "nav.logs" },
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
    if (msg.role === "tool" || msg.type === "tool_call") {
      // 工具消息不回灌会让"继续/重试"变成失忆的全新请求（P0-2）：
      // 转成 user 角色 [工具结果] 回灌，模型知道之前查过什么
      const raw = (msg.toolResult || msg.content || "").toString();
      const preview = raw.length > 1000 ? raw.slice(0, 1000) + "\n...(结果过长已截断)" : raw;
      if (preview.trim()) {
        msgs.push({
          role: "user",
          content: `[工具结果] ${msg.toolName || ""} ${JSON.stringify(msg.toolArgs || {}).slice(0, 200)}\n${preview}`,
        });
      }
      continue;
    }
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

  // 输出反思档位：off | light | deep（默认 light，仅云端长输出触发）
  const [reflectionMode, setReflectionMode] = useState<"off" | "light" | "deep">(
    () => (localStorage.getItem("latiao_reflection") as "off" | "light" | "deep") || "light"
  );
  useEffect(() => { localStorage.setItem("latiao_reflection", reflectionMode); }, [reflectionMode]);

  // 权限模式五档（从保守到放手）：read_only / confirm / auto_edit / plan / full
  // 思考强度三档（🧠 选择器）：off / high(默认) / max
  const [thinkingLevel, setThinkingLevel] = useState<"off" | "high" | "max">(
    () => (localStorage.getItem("latiao_thinking") as "off" | "high" | "max") || "high"
  );
  useEffect(() => { localStorage.setItem("latiao_thinking", thinkingLevel); }, [thinkingLevel]);

  const [accessMode, setAccessMode] = useState<"read_only" | "confirm" | "auto_edit" | "plan" | "full">(() => {
    const saved = localStorage.getItem("latiao_access");
    if (!saved) {
      // 迁移旧版独立"计划模式"开关
      try { if (JSON.parse(localStorage.getItem("local_ai_os_plan_mode") || "false")) return "plan"; } catch { /* ignore */ }
      return "full";
    }
    // 旧版本值迁移：workspace → auto_edit
    if (saved === "workspace") return "auto_edit";
    return (["read_only", "confirm", "auto_edit", "plan", "full"].includes(saved) ? saved : "full") as "read_only" | "confirm" | "auto_edit" | "plan" | "full";
  });
  useEffect(() => { localStorage.setItem("latiao_access", accessMode); }, [accessMode]);


  const [prompt, setPrompt] = useState("");
  const [isProcessing, setIsProcessing] = useState(false);
  // 当前执行中任务摘要（tool_start/tool_end 驱动，顶部状态条展示）
  const [activeTask, setActiveTask] = useState<string | null>(null);
  // 后台子智能体任务（ZCode 式活动栏：delegate_task background=true 产生）
  const [subagents, setSubagents] = useState<{ id: string; agent: string; task: string; status: string; steps?: number; activity?: Record<string, number>; last_activity?: string; summary?: string }[]>([]);
  const [taskStartAt, setTaskStartAt] = useState<number | null>(null);
  const activeTaskStackRef = useRef<string[]>([]);
  const abortControllerRef = useRef<AbortController | null>(null);
  // 已提示过的 cron 完成事件（ts+task 键），防止 5s 心跳对同一事件反复弹 toast
  const lastCronEventRef = useRef<string>("");
  // useSessions 的 setter 每次渲染重建，心跳 effect（空依赖）需要最新引用
  const setSessionsRef = useRef(setSessions);
  setSessionsRef.current = setSessions;
  const setCurrentIdxRef = useRef(setCurrentIdx);
  setCurrentIdxRef.current = setCurrentIdx;
  const setActiveViewRef = useRef(setActiveView);
  setActiveViewRef.current = setActiveView;
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
  // 连续心跳失败计数：sidecar 冷启动需 1-2 分钟，单次失败不判离线，
  // 连续 12 次(≈60s)才显示恢复面板，避免启动竞态误报
  const offlineStreakRef = useRef(0);
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
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
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
  // 边录边转写: 录音中是否仍在进行 + 上一块是否还在识别(串行,避免堆积)
  const isRecordingRef = useRef(false);
  const transcribingRef = useRef(false);
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
        // imagePreview 也是完整 data URL（~100-300KB/张）——不剥离的话几张截图
        // 就会撑爆 localStorage 配额，触发回退逻辑丢掉全部旧会话
        imagePreview: undefined,
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

  // Auto-scroll chat to bottom（instant，内容高度未定时 smooth 会滚错位）
  useEffect(() => {
    const timer = setTimeout(() => {
      chatEndRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
    }, 120);
    return () => clearTimeout(timer);
  }, [messages]);
  // 会话切换/首次加载：立即滚到底（不含历史消息变化时的 smooth 竞态）
  useEffect(() => {
    const timer = setTimeout(() => {
      chatEndRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
    }, 60);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentIdx, session.id]);
  // Persist cloud models to OS keychain (debounced to avoid writes on every keystroke)
  useEffect(() => {
    if (!cloudModelsLoaded) return;
    const timer = setTimeout(async () => {
      try {
        await invoke("store_secret", { key: "cloud_models", value: JSON.stringify(cloudModels) });
        // 同步一份到 sidecar config.json：cron 定时任务/自动路由在后台运行，
        // 拿不到每次请求携带的 cloud_config，必须从持久化配置读取。
        // sidecar 冷启动可能尚未就绪 -> waitForSidecar 等待后重试
        const { sidecarFetchWithRetry } = await import("./utils/api");
        await sidecarFetchWithRetry("/v1/settings/cloud-models", "POST", { models: cloudModels }, 3);
      } catch (e) { console.warn("Failed to persist cloud models to keychain", e); }
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
        invoke("plugin:opener|open_url", { url: a.href }).catch(() => {});
      } catch { /* malformed href — not a link worth opening */ }
    };
    document.addEventListener("click", handler, true);
    return () => document.removeEventListener("click", handler, true);
  }, []);


  const [fetchDiag, setFetchDiag] = useState("🔍 正在获取...");


  // Fetch capabilities (统一能力模型：工具+技能) from sidecar (via Rust IPC proxy) with health check + retry
  const fetchCapabilities = async () => {
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
          setFetchDiag(`⏳ 重试获取能力列表... (${attempt}/${maxRetries})`);
          await new Promise(r => setTimeout(r, 2000));
        } else {
          setFetchDiag("⏳ 正在获取能力列表...");
        }
        const data = await sidecarFetch("/v1/capabilities");
        setFetchDiag(`✅ /v1/capabilities → status=${data.status}`);
        if (data.status === "ok") {
          setCapabilities(data.capabilities || []);
          setFetchDiag(d => `${d}, capabilities=${data.capabilities?.length || 0}`);
        }
        return; // success
      } catch (e: any) {
        if (attempt === maxRetries - 1) {
          setFetchDiag(`❌ 错误: ${e?.message || String(e)} (已重试${maxRetries}次)`);
        }
      }
    }
  };
  useEffect(() => { fetchCapabilities(); }, []);

  // Fetch recent logs (supplier for recovery panel)
  const fetchGatewayLogs = async () => {
    try {
      const lr = await authFetch("/v1/logs?limit=100", { signal: AbortSignal.timeout(5000) });
      const ld = await lr.json();
      if (ld.status === "ok") setGatewayLogs(ld.logs || []);
    } catch { /* ignore */ }
  };

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
        const resp = await authFetch("/v1/heartbeat", { signal: AbortSignal.timeout(5000) });
        const data = await resp.json();
        if (data.status === "ok") {
          offlineStreakRef.current = 0;
          setSidecarStatus("online");
          // Guard: never overwrite a good status with an undefined payload field
          if (data.local_llm != null) setLocalLLMStatus(data.local_llm);
          // Downloads are owned by the 2s poller below (single writer, no flicker)
          // Learnings
          setRecentLearnings(data.learnings || []);
          // 后台子智能体任务快照
          setSubagents((data.subagents || []) as typeof subagents);
          // Cron completion toasts (skip "skipped" to avoid spam)
          for (const ev of (data.cron_events || []) as { ts: string; task: string; status: string; summary?: string; full?: string }[]) {
            const key = `${ev.ts}|${ev.task}`;
            if (ev.status !== "skipped" && key > lastCronEventRef.current) {
              lastCronEventRef.current = key;
              // 跨重启去重：已显示过的事件不再重复弹 toast / 插入聊天
              let shown: string[] = [];
              try { shown = JSON.parse(localStorage.getItem("latiao_cron_shown") || "[]"); } catch { /* ignore */ }
              if (shown.includes(key)) continue;
              shown.push(key);
              if (shown.length > 100) shown.splice(0, shown.length - 100);
              localStorage.setItem("latiao_cron_shown", JSON.stringify(shown));
              const header = t(ev.status === "error" ? "toast.cron_fail" : "toast.cron_done", { task: ev.task });
              showToast(header, ev.status === "error" ? "warn" : undefined);
              // 自动新建专属聊天会话，完整结果写入其中（不混入当前对话）
              const content = `**${header}**\n\n${(ev.full || ev.summary || "").trim() || "(无输出)"}`;
              const s = newSession();
              const name = `⏰ ${ev.task.replace(/[🔍📋📊⚡📈]|\s*\(记录到记忆库\)/g, "").trim().slice(0, 20)} ${ev.ts.slice(5, 16).replace("T", " ")}`;
              const sess = { ...s, name, messages: [{ id: msgId(), role: "assistant" as const, content, ts: Date.now() }], lastActive: Date.now() };
              // 新会话插到列表顶部并切换到聊天页，确保用户立刻看得到
              setSessionsRef.current((prev) => [sess, ...prev]);
              setCurrentIdxRef.current(0);
              setActiveViewRef.current("chat");
            }
          }
        } else {
          offlineStreakRef.current += 1;
          if (offlineStreakRef.current >= 18) setSidecarStatus("offline");
        }
      } catch {
        offlineStreakRef.current += 1;
        if (offlineStreakRef.current >= 18) setSidecarStatus("offline");
      }

      // Fetch recent logs (always, cheap ring-buffer read)
      await fetchGatewayLogs();
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
  // Mirror for the poll loop: lets the interval check current progress without
  // re-subscribing on every state change.
  const downloadProgressRef = useRef(downloadProgress);
  downloadProgressRef.current = downloadProgress;
  const [fixing, setFixing] = useState("");
  const [contextLimit, setContextLimit] = useState(8192);
  const [contextEstimate, setContextEstimate] = useState<{max_context: number; recommended_context: number; ram_available_gb: number; memory_for_context_gb: number} | null>(null);

  // Fetch context estimate
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
  useEffect(() => { fetchContextEstimate(); }, []);

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
          setDownloadProgress(progress);
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
    if (!hfSearch.trim()) { setHfResults([]); return; }
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
    await authFetch("/v1/local-llm/download/pause", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_id: modelId }),
    });
  };

  const resumeDownload = async (modelId: string) => {
    await authFetch("/v1/local-llm/download/resume", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_id: modelId }),
    });
  };

  const cancelDownload = async (modelId: string) => {
    await authFetch("/v1/local-llm/download/cancel", {
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
      const resp = await authFetch("/v1/local-llm/start", {
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
      const resp = await authFetch("/v1/local-llm/stop", { method: "POST" });
      const data = await resp.json();
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
  const switchSession = (idx: number) => { setCurrentIdx(idx); setPendingFile(null); setActiveView("chat"); };
  const deleteSession = (idx: number) => {
    const makeNew = () => ({ id: `session_${Math.random().toString(36).substring(7)}`, name: "session.default", messages: [] as Message[], selectedModel: "", lastActive: Date.now() });
    setSessions((prev) => {
      const next = prev.filter((_, i) => i !== idx);
      if (next.length === 0) return [makeNew()];
      return next;
    });
    if (currentIdx >= idx) setCurrentIdx((c) => Math.max(0, c - 1));
  };
  /* ── Toast ── */
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showToast = useCallback((msg: string, type?: string) => {
    setToast(msg);
    setToastType(type || "info");
    // Clear any previous auto-dismiss timer so a new toast isn't cut short
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    toastTimerRef.current = setTimeout(() => setToast(null), 2200);
  }, []);

  /* ── 自动更新（此前零接线：死开关 + 硬编码版本号，审计修复）── */
  const [appVersion, setAppVersion] = useState("…");
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const runUpdateCheck = useCallback(async (silent: boolean) => {
    if (checkingUpdate) return;
    setCheckingUpdate(true);
    const { checkForUpdates } = await import("./utils/updater");
    const res = await checkForUpdates((msg) => showToast(msg), !silent);
    setCheckingUpdate(false);
    if (!silent && res === "none") showToast("已是最新版本");
  }, [checkingUpdate, showToast]);
  useEffect(() => {
    import("./utils/updater").then(({ getAppVersion }) => {
      getAppVersion().then(setAppVersion).catch(() => setAppVersion("0.3.1"));
    }).catch(() => setAppVersion("0.3.1"));
    if (autoCheckUpdate) runUpdateCheck(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Extracted hooks (cron — depends on showToast)
  const { cronJobs, newCron, setNewCron, addCronJob, toggleCronJob, deleteCronJob, runCronJob } = useCronJobs(showToast);

  /* ── Stream Chat (preserved from original) ── */
  const streamChat = async (
    messages: Record<string, unknown>[],
    opts?: { model?: string; agent?: string; cloudConfig?: Record<string, unknown>; skipTools?: boolean; sessionId?: string },
    signal?: AbortSignal,
  ): Promise<string> => {
    const body: Record<string, unknown> = { messages, stream: true, reflection_mode: reflectionMode, access_mode: accessMode, thinking_level: thinkingLevel };
    // 传 session_id：后端记忆/停滞检测按会话归档（P0-2）
    if (opts?.sessionId) body.session_id = opts.sessionId;
    if (opts?.model) body.model = opts.model;
    if (opts?.agent) body.agent = opts.agent;
    if (opts?.cloudConfig) body.cloud_config = opts.cloudConfig;
    if (opts?.skipTools) body.skip_tools = true;

    let response: Response;
    try {
      response = await authFetch("/v1/chat/completions", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal,
      });
    } catch (e) {
      throw new Error(`无法连接 Sidecar\n原始错误: ${e}`, { cause: e });
    }
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "", full = "";

    // 流式渲染节流：内容/思考按 120ms 批量落盘。每条 token 都 setMessages 会
    // 让长会话（大量 ReactMarkdown）全量重渲染占满主线程——停止按钮点击排队
    // 等不到主线程，"停止没反应"的根因。
    let flushTimer: ReturnType<typeof setTimeout> | null = null;
    let pendingThinking = "";
    // reflection_revised 已把最终文本写入消息；此后 [DONE]/finally 的 flushStream
    // 若再跑，prefix 检查不匹配会把 revised 文本重复 push 一条（M1 复发）。
    let streamFinalized = false;
    const flushStream = () => {
      if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
      if (streamFinalized) return;
      const text = full;
      const th = pendingThinking;
      pendingThinking = "";
      if (!text && !th) return;
      setMessages((prev) => {
        const msgs = [...prev];
        const last = msgs[msgs.length - 1];
        if (last?.role === "assistant") {
          if (text && last.content && !text.startsWith(last.content)) {
            // 已有内容的 assistant（如 📋 执行计划）：正文另起新消息，不覆盖
            msgs.push({ id: msgId(), role: "assistant", content: text, ts: Date.now() });
          } else {
            const updated: Message = { ...last };
            if (th) updated.thinking = (last.thinking || "") + th;
            if (text) updated.content = text;
            // 正文开始输出 = 思考结束，结算思考耗时
            if (text && updated.thinkingDuration === undefined && updated.thinking) {
              updated.thinkingDuration = Math.max(0, Date.now() - (updated.ts || Date.now()));
            }
            msgs[msgs.length - 1] = updated;
          }
        } else if (text || th) {
          msgs.push({ id: msgId(), role: "assistant", content: text, thinking: th || undefined, ts: Date.now() });
        }
        return msgs;
      });
    };
    const scheduleFlush = () => {
      if (!flushTimer) flushTimer = setTimeout(flushStream, 120);
    };


    // Inactivity watchdog: if the stream goes silent for too long (e.g. the
    // local model server hangs on an unsupported request, or a tool call blocks
    // without emitting events), abort so isProcessing always resets instead of
    // leaving the red Stop button stuck forever. Reset on every received chunk.
    const WATCHDOG_MS = 180_000;  // 本地模型 prefill 可能 60-100s 无数据，90s 会误断流
    let watchdog: ReturnType<typeof setTimeout> | null = null;
    let watchdogFired = false;
    const armWatchdog = () => {
      if (watchdog) clearTimeout(watchdog);
      watchdog = setTimeout(() => {
        watchdogFired = true;
        try { reader.cancel("watchdog-timeout").catch(() => {}); } catch { /* noop */ }
      }, WATCHDOG_MS);
    };
    const disarmWatchdog = () => { if (watchdog) { clearTimeout(watchdog); watchdog = null; } };
    armWatchdog();

    try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        // watchdog 触发的 cancel 会让 read() 以 done:true 正常结束——
        // 若不当报错，截断的回答看起来像"自然结束"，用户无从分辨
        if (watchdogFired) throw new Error("⏱ 流式响应超时（180s 无数据），已中断。可重试或检查模型服务状态。");
        break;
      }
      armWatchdog();
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (line.startsWith("data: ")) {
          const data = line.substring(6).trim();
          if (data === "[DONE]") { flushStream(); return full; }
          try {
            const parsed = JSON.parse(data);
            // Error events must reach the outer catch (and finally sendMessage's
            // ❌ error display) instead of being swallowed as "malformed event".
            if (parsed.error) throw new Error(parsed.error);
            try {
              if (parsed.event === "tool_confirm") {
                flushStream();
                setAgentPhase(t("agent.phase_confirm", { tool: parsed.tool || "" }));
                showToast(t("tool.confirm_toast", { tool: parsed.tool || "" }), "warn");
                // 等待用户确认可能超过看门狗时限——确认期间暂停看门狗，
                // 用户点允许/拒绝后 tool_start/tool_end 数据到达即自动恢复
                disarmWatchdog();
                setMessages((prev) => {
                  const msgs = [...prev];
                  // 工具消息插到用户问题之后、思考/回答之前（保持 [user, tool, assistant] 顺序）
                  const last = msgs[msgs.length - 1];
                  const idx = last?.role === "assistant" ? msgs.length - 1 : msgs.length;
                  msgs.splice(idx, 0, {
                    id: msgId(), role: "tool", type: "tool_call", content: "",
                    callId: parsed.call_id, toolName: parsed.tool, toolArgs: parsed.args, toolStatus: "confirming",
                  });
                  return msgs;
                });
              } else if (parsed.event === "agent_plan") {
                flushStream();
                // 规划模式：执行计划显示为一条消息
                const plan = String(parsed.content ?? "");
                if (plan.trim()) {
                  setMessages((prev) => [...prev, {
                    id: msgId(), role: "assistant",
                    content: `📋 **执行计划**\n\n${plan}`,
                  }]);
                }
              } else if (parsed.event === "plan_confirm") {
                // 计划确认门控：批准后才开始执行（后端在等这个 call_id 的决定）
                flushStream();
                disarmWatchdog();
                showToast(t("tool.confirm_toast", { tool: "执行计划" }), "warn");
                setAgentPhase(t("agent.phase_confirm", { tool: "执行计划" }));
                setMessages((prev) => [...prev, {
                  id: msgId(), role: "tool", type: "tool_call", content: "",
                  callId: parsed.call_id, toolName: "执行计划",
                  toolArgs: parsed.args, toolStatus: "confirming",
                }]);
              } else if (parsed.event === "reflection_revised") {
                flushStream();
                // 输出反思修正：把最后一条 assistant 消息替换为修正版。
                // 同步 full = revised，防止 [DONE] 时最终 flush 把修正前原文
                // 又以新气泡重复显示（M1 复盘 bug）。
                const revised = String(parsed.content ?? "");
                if (revised.trim()) {
                  full = revised;
                  streamFinalized = true; // 后续 [DONE]/finally flush 不再跑（否则 revised 被重复 push）
                  setMessages((prev) => {
                    const msgs = [...prev];
                    for (let i = msgs.length - 1; i >= 0; i--) {
                      if (msgs[i].role === "assistant" && msgs[i].content && msgs[i].content.trim()) {
                        msgs[i] = { ...msgs[i], content: revised + "\n\n_✍️ 已自查修正_" };
                        break;
                      }
                    }
                    return msgs;
                  });
                }
              } else if (parsed.event === "content_revised") {
                // 追问续写轮：把最后一条 assistant 消息替换为当前累积文本。
                // 与 reflection_revised 的区别：不加"已自查修正"角标。
                flushStream();
                const revised = String(parsed.content ?? "");
                if (revised.trim()) {
                  full = revised;
                  streamFinalized = true;
                  setMessages((prev) => {
                    const msgs = [...prev];
                    for (let i = msgs.length - 1; i >= 0; i--) {
                      if (msgs[i].role === "assistant" && msgs[i].content && msgs[i].content.trim()) {
                        msgs[i] = { ...msgs[i], content: revised };
                        break;
                      }
                    }
                    return msgs;
                  });
                }
              } else if (parsed.event === "tool_start") {
                flushStream();
                // 工具执行期静默可超 180s（长命令/重载）：暂停看门狗，tool_end 再续（P0-4）
                disarmWatchdog();
                activeTaskStackRef.current.push(`${parsed.tool || ""} ${JSON.stringify(parsed.args || {}).slice(0, 60)}`);
                setActiveTask(activeTaskStackRef.current[activeTaskStackRef.current.length - 1] || null);
                const startTs = Number(parsed.ts) || Date.now();
                setMessages((prev) => {
                  const msgs = [...prev];
                  const idx = msgs.findIndex((m) => m.callId === parsed.call_id && m.toolStatus === "confirming");
                  if (idx !== -1) { msgs[idx] = { ...msgs[idx], toolStatus: "running", ts: startTs }; }
                  else {
                    const last = msgs[msgs.length - 1];
                    const pos = last?.role === "assistant" ? msgs.length - 1 : msgs.length;
                    msgs.splice(pos, 0, {
                      id: msgId(), role: "tool", type: "tool_call", content: "",
                      callId: parsed.call_id, toolName: parsed.tool, toolArgs: parsed.args, toolStatus: "running",
                      ts: startTs,
                    });
                  }
                  return msgs;
                });
              } else if (parsed.event === "tool_end") {
                flushStream();
                armWatchdog();  // 工具执行结束，恢复看门狗（P0-4）
                activeTaskStackRef.current.pop();
                setActiveTask(activeTaskStackRef.current[activeTaskStackRef.current.length - 1] || null);
                const rawResult = String(parsed.result ?? "");
                const toolResult = rawResult.length > 10000
                  ? rawResult.slice(0, 10000) + `\n\n...(截断)`
                  : rawResult;
                const isError = rawResult.startsWith("Error") || rawResult.startsWith("⛔");
                const endTs = Number(parsed.ts) || Date.now();
                setMessages((prev) => {
                  const msgs = [...prev];
                  const idx = msgs.findIndex((m) => m.callId === parsed.call_id && (m.toolStatus === "running" || m.toolStatus === "confirming"));
                  if (idx !== -1) {
                    const base = msgs[idx];
                    msgs[idx] = {
                      ...base, toolResult, toolStatus: isError ? "error" : "done", content: toolResult,
                      duration: base.ts ? Math.max(0, endTs - base.ts) : undefined,
                    };
                  }
                  return msgs;
                });
              } else if (parsed.reasoning) {
                // 思考内容：批量累积（flush 时写入最后一条 assistant 的 thinking 字段）
                pendingThinking += String(parsed.reasoning);
                scheduleFlush();
              } else if (parsed.content) {
                // 追问轮（content_revised）之后若又出现新一轮正常内容
                // （如工具调用后的新回答）：重置累积，另起新气泡，
                // 不再把替换文本与新内容拼在一起
                if (streamFinalized) { full = ""; streamFinalized = false; }
                full += parsed.content;
                scheduleFlush();
              }
            } catch (e) { console.warn("Skipping malformed stream event", e); }
          } catch (e) { if (e instanceof SyntaxError) continue; throw e; }
        }
      }
    }
    return full;
    } finally {
      flushStream();
      disarmWatchdog();
      // Proactively release the stream: plugin-http holds the response body as a
      // Tauri resource (rid). If we just walk away, the plugin's later teardown
      // races with connection close and rejects "The resource id ... is invalid"
      // as an unhandled promise rejection -> fullscreen crash overlay.
      try { await reader.cancel("stream-done"); } catch { /* already closed */ }
      try { reader.releaseLock(); } catch { /* noop */ }
    }
  };

  const confirmTool = useCallback(async (callId: string, approved: boolean) => {
    try {
      const resp = await authFetch("/v1/confirm_tool", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ call_id: callId, approved }),
      });
      const data = await resp.json();
      if (data.status === "not_found") {
        showToast(t("toast.timeout"));
        setMessages(prev => prev.map(m => m.callId === callId && m.toolStatus === "confirming" ? { ...m, toolStatus: "error" as const, toolResult: t("toast.timeout_detail") } : m));
      }
    } catch (e) { console.error(e); showToast(t("toast.confirm_fail")); }
  }, [showToast, setMessages, t]);

  const stopGeneration = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    activeTaskStackRef.current = [];
    setActiveTask(null);
    setTaskStartAt(null);
    setIsProcessing(false);
    setPendingFile(null);
    setAgentPhase("");
  }, []);

  /* ── Send Message ── */
  const sendMessage = async () => {
    const text = prompt;
    if (!text.trim() && !pendingFile) return;
    // Re-entrancy guard: if a previous request is still streaming, ignore
    // duplicate sends (double-click, repeated Enter, etc.). Without this,
    // concurrent agent loops pile up (msg_count doubles each send), the
    // local model server gets hammered by parallel requests + retry storms,
    // and the sidecar eventually crashes -> "Unhandled Promise Rejection".
    if (isProcessing) return;

    // Guard: block sending images to a local model that lacks vision support.
    // Otherwise the local server (mlx_lm/llama.cpp) hangs or errors on the
    // image_url content array, the stream never closes cleanly, and
    // isProcessing stays true forever (red Stop button never reverts).
    const cloudCfgPre = session.selectedModel
      ? cloudModels.find((m) => m.name === session.selectedModel)
      : undefined;
    const isLocalTarget = !cloudCfgPre;
    if (pendingFile?.base64 && pendingFile.type === "image" && isLocalTarget && !localLLMStatus?.has_image_support) {
      showToast(t("toast.no_vision"), "warn");
      setPendingFile(null);
      return;
    }

    setPrompt("");
    setIsProcessing(true);
    setAgentPhase(t("agent.phase_analyze"));
    activeTaskStackRef.current = []; // 清残留工具栈（上次中断/未正常结束）
    setActiveTask(null);
    setTaskStartAt(Date.now()); // 任务头部"已工作"计时起点

    const userMsg: Message = { id: msgId(), role: "user", content: text || "Analyze this file", ts: Date.now() };
    if (pendingFile) {
      userMsg.type = pendingFile.type === "image" ? "image" : "file";
      userMsg.filename = pendingFile.name;
      if (pendingFile.base64) {
        userMsg.imageBase64 = pendingFile.base64;
        userMsg.imageMime = pendingFile.mimeType;
        // imagePreview drives the in-bubble thumbnail (ChatView.tsx); without it
        // the screenshot history collapses to a "[File: ...]" text line.
        if (pendingFile.type === "image" && pendingFile.preview) userMsg.imagePreview = pendingFile.preview;
      }
      if (pendingFile.type === "image") {
        userMsg.content = text || `[Image: ${pendingFile.name}]`;
      } else {
        // 文本附件必须把内容真正发给模型（此前只发占位符，模型看不到文件）。
        // 太长截断：上下文有限，128KB 足够覆盖绝大多数源码/文档。
        const MAX_FILE_CHARS = 128 * 1024;
        let body = (pendingFile.content || "").slice(0, MAX_FILE_CHARS);
        if ((pendingFile.content || "").length > MAX_FILE_CHARS) body += "\n\n...(文件过长已截断)";
        userMsg.content =
          (text ? text + "\n\n" : "") +
          `📎 文件「${pendingFile.name}」内容如下：\n\n\`\`\`\n${body}\n\`\`\``;
      }
    }

    setMessages((prev) => [...prev, userMsg]);

    const assistantPlaceholder: Message = { id: msgId(), role: "assistant", content: "", ts: Date.now() };
    setMessages((prev) => [...prev, assistantPlaceholder]);

    try {
      const apiMessages = buildApiMessages(session, userMsg, accessMode === "plan", lang);
      const opts: Record<string, unknown> = { agent: activeAgent, sessionId: session.id };
      if (session.selectedModel) opts.model = session.selectedModel;
      if (cloudCfgPre) opts.cloudConfig = { key: cloudCfgPre.key, endpoint: cloudCfgPre.endpoint, protocol: cloudCfgPre.protocol || "openai" };

      const controller = new AbortController();
      abortControllerRef.current = controller;
      await streamChat(apiMessages, opts, controller.signal);
    } catch (e) {
      // Tauri plugin-http teardown after abort rejects with "The resource id
      // ... is invalid" instead of a standard AbortError — treat it the same.
      const errText = String((e as { message?: string })?.message ?? e ?? "");
      const aborted = (e as { name?: string })?.name === "AbortError"
        || /resource id .* is invalid/i.test(errText);
      setMessages((prev) => {
        const msgs = [...prev];
        const last = msgs[msgs.length - 1];
        if (last?.role === "assistant") {
          if (aborted) {
            // User pressed Stop: keep whatever was already generated instead of
            // overwriting it with an error. Drop only a still-empty placeholder.
            if (last.content) msgs[msgs.length - 1] = { ...last, content: `${last.content}\n\n(已停止)` };
            else msgs.pop();
          } else {
            msgs[msgs.length - 1] = { ...last, content: `❌ ${e}` };
          }
        }
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
    } else if (file.type === "application/pdf") {
      // PDF 无法作为 image_url 发给模型（视觉模型也解不了 PDF），也没有
      // 内置解析器——直接提示不支持，避免用户等 180s 超时。
      showToast(t("toast.pdf_unsupported"), "warn");
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    } else {
      const reader = new FileReader();
      reader.onload = () => {
        const result = reader.result as string;
        setPendingFile({ name: file.name, preview: "📄", type: "file", content: result });
      };
      reader.readAsText(file);
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
      let mediaRecorder: MediaRecorder;
      try {
        mediaRecorder = new MediaRecorder(stream, { mimeType: "audio/webm" });
      } catch (e) {
        // Construction failed (e.g. unsupported mimeType) — release the mic
        stream.getTracks().forEach((t) => t.stop());
        throw e;
      }
      mediaRecorderRef.current = mediaRecorder;
      audioChunksRef.current = [];
      isRecordingRef.current = true;

      // 录音中每 3 秒一块,边录边转写追加到输入框(停止后用完整音频覆盖为最终准确文本)
      const blobToBase64 = (blob: Blob): Promise<string> => new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve((reader.result as string).split(",")[1] || "");
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(blob);
      });
      const transcribeChunk = async (blob: Blob) => {
        if (transcribingRef.current) return; // 上一块还在识别,跳过这块避免堆积
        transcribingRef.current = true;
        try {
          const base64 = await blobToBase64(blob);
          const resp = await authFetch("/v1/recognize_speech", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ audio_base64: base64, mime_type: "audio/webm" }),
          });
          const data = await resp.json();
          if (data.status === "success" && data.text && data.text !== "(未识别到语音内容)") {
            setPrompt(prev => (prev ? prev + " " : "") + data.text);
          }
        } catch { /* 实时块识别失败静默,最终完整识别兜底 */ }
        finally { transcribingRef.current = false; }
      };

      mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          audioChunksRef.current.push(event.data);
          if (isRecordingRef.current) transcribeChunk(event.data);
        }
      };
      mediaRecorder.onstop = async () => {
        isRecordingRef.current = false;
        setIsRecording(false);
        const audioBlob = new Blob(audioChunksRef.current, { type: "audio/webm" });
        const base64 = await blobToBase64(audioBlob);

        try {
          const resp = await authFetch("/v1/recognize_speech", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ audio_base64: base64, mime_type: "audio/webm" }),
          });
          const data = await resp.json();
          if (data.text) {
            // 完整音频识别更准 → 覆盖实时追加的文本
            setPrompt(data.text);
          }
        } catch (e) { console.error(e); showToast(t("toast.speech_fail")); }
        stream.getTracks().forEach((t) => t.stop());
      };
      mediaRecorder.start(3000); // 3s 一块,支持边录边转写
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
      const resp = await authFetch("/v1/test_connection", {
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
          {sidebarCollapsed
            ? <img className="sidebar-logo" src={logoUrl} alt="辣条" />
            : <div className="sidebar-logo"><img src={logoUrl} alt="辣条" /></div>}
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
                <div className="session-preview">{s.messages.length > 0 ? (s.messages[s.messages.length - 1].content || "").replace(/[#*|`>-]/g, " ").replace(/\s+/g, " ").slice(0, 30) + "..." : t("session.default")}</div>
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
              <span className="nav-icon"><item.icon size={17} strokeWidth={1.8} /></span> {!sidebarCollapsed && t(item.key)}
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          <select className="sidebar-model-select" value={session.selectedModel} onChange={(e) => { setSelectedModel(e.target.value); showToast(t("toast.model_switched", { model: e.target.value || t("sidebar.auto_detect") })); }}>
            <option value="">{t("sidebar.auto_detect")}</option>
            {cloudModels.map((m) => (<option key={m.name} value={m.name}>☁️ {m.name}</option>))}
          </select>
        </div>
      </aside>

      {/* ═══ Main ═══ */}
      <main className="main" style={{ position: "relative" }}>
        <div className="topbar">
          <span className="topbar-title">{session.name}</span>
          <span style={{ fontSize: 10, color: "var(--text-muted)", padding: "2px 6px", borderRadius: "var(--radius-sm)", background: "var(--accent-soft)", marginRight: 8 }}>
            {t(AGENT_NAME_KEYS[activeAgent] || activeAgent)}
          </span>
          <span className={`status-dot ${sidecarStatus === "online" ? "online" : "offline"}`}></span>
          <span className="status-label">{sidecarStatus === "online" ? t("sidebar.online") : t("sidebar.offline")}</span>
          {isProcessing && (
            <span style={{ fontSize: 11, color: agentPhase ? "var(--accent)" : "var(--text-muted)", marginLeft: 8, padding: "2px 8px", borderRadius: "var(--radius-sm)", background: agentPhase ? "var(--accent-soft)" : "transparent" }}>
              {agentPhase || t("sidebar.processing")}
            </span>
          )}
        </div>


        {/* ═══ Chat View ═══ */}
        {sidecarStatus === "offline" && (
          <div className="view-panel active" id="view-recovery" style={{ flex: 1 }}>
            <RecoveryView
              sidecarStatus={sidecarStatus}
              restartingSidecar={restartingSidecar}
              onRestartSidecar={handleRestartSidecar}
              gatewayLogs={gatewayLogs}
              fetchLogs={() => void fetchGatewayLogs()}
            />
          </div>
        )}
        <div className={`view-panel${activeView === "chat" ? " active" : ""}`} id="view-chat" style={sidecarStatus === "offline" ? { display: "none" } : undefined}>
          <ChatView
            messages={messages} isProcessing={isProcessing}
            pendingFile={pendingFile} setPendingFile={setPendingFile}
            prompt={prompt} setPrompt={setPrompt}
            fileInputRef={fileInputRef} mediaRecorderRef={mediaRecorderRef}
            isRecording={isRecording}
            sendMessage={sendMessage} onStop={stopGeneration} handleFileSelect={handleFileSelect}
            startRecording={startRecording} confirmTool={confirmTool}
            chatEndRef={chatEndRef} handleDrop={handleDrop}
            onPasteImage={(file) => processImageFile(file, `截图 ${new Date().toLocaleTimeString()}`)}
            cloudModels={cloudModels}
            selectedModel={session.selectedModel}
            onSelectModel={setSelectedModel}
            accessMode={accessMode} setAccessMode={setAccessMode}
            thinkingLevel={thinkingLevel} setThinkingLevel={setThinkingLevel}
            contextEstimate={contextEstimate}
            showToast={showToast}
            activeTask={activeTask}
            taskStartAt={taskStartAt}
            subagents={subagents}
          />
        </div>

        {/* ═══ Models View ═══ */}
        <div className={`view-panel${activeView === "models" ? " active" : ""}`} id="view-models" style={sidecarStatus === "offline" ? { display: "none" } : undefined}>
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

        {/* ═══ Tools View（统一能力模型：工具+技能一套管理） ═══ */}
        <div className={`view-panel${activeView === "tools" ? " active" : ""}`} id="view-tools" style={sidecarStatus === "offline" ? { display: "none" } : undefined}>
          <div className="page-header">
            <div><div className="page-title">{t("page.tools")}</div><div className="page-desc">{t("page.tools_desc", { count: capabilities.filter(c => c.kind === "tool").length, skills_enabled: capabilities.filter(c => c.kind === "skill" && c.enabled).length, skills_total: capabilities.filter(c => c.kind === "skill").length })}</div></div>
          </div>
          <div className="page-body">
            {capabilities.length === 0 && <div style={{padding:20,color:'var(--warning)',fontFamily:'monospace',whiteSpace:'pre-wrap'}}>{fetchDiag}</div>}
            <ToolsView capabilities={capabilities} setCapabilities={setCapabilities} showToast={showToast} />
          </div>
        </div>


        {/* ═══ Cron View ═══ */}
        <div className={`view-panel${activeView === "cron" ? " active" : ""}`} id="view-cron" style={sidecarStatus === "offline" ? { display: "none" } : undefined}>
          <div className="page-header">
            <div><div className="page-title">{t("page.cron")}</div><div className="page-desc">{t("page.cron_desc", { count: cronJobs.filter(j => j.enabled).length })}</div></div>
          </div>
          <div className="page-body">
            <CronView key={lang} cronJobs={cronJobs} newCron={newCron} setNewCron={setNewCron}
              toggleCronJob={toggleCronJob} deleteCronJob={deleteCronJob} addCronJob={addCronJob} runCronJob={runCronJob} />
          </div>
        </div>
        {/* ═══ Channels View ═══ */}
        <div className={`view-panel${activeView === "channels" ? " active" : ""}`} id="view-channels" style={sidecarStatus === "offline" ? { display: "none" } : undefined}>
          <div className="page-header">
            <div><div className="page-title">{t("page.channels")}</div><div className="page-desc">{t("page.channels_desc")}</div></div>
          </div>
          <div className="page-body">
            <ChannelsView />
          </div>
        </div>


        {/* ═══ Agent View ═══ */}
        <div className={`view-panel${activeView === "agents" ? " active" : ""}`} id="view-agents" style={sidecarStatus === "offline" ? { display: "none" } : undefined}>
          <div className="page-header">
            <div><div className="page-title">{t("page.agents")}</div><div className="page-desc">{t("page.agents_desc")}</div></div>
            <button className="btn btn-md btn-primary" style={{ marginLeft: "auto" }} onClick={() => showToast(t("agent.created_simple"))}>{t("agent.new_btn")}</button>
          </div>
          <div className="page-body">
            <AgentView key={lang} activeAgent={activeAgent} setActiveAgent={setActiveAgent} showToast={showToast} />
          </div>
        </div>
        <div className={`view-panel${activeView === "settings" ? " active" : ""}`} id="view-settings" style={sidecarStatus === "offline" ? { display: "none" } : undefined}>
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
            appVersion={appVersion} checkingUpdate={checkingUpdate} onCheckUpdate={() => runUpdateCheck(false)}
            reflectionMode={reflectionMode} setReflectionMode={setReflectionMode}
          />
        </div>

        {/* ═══ Logs View ═══ */}
        <div className={`view-panel${activeView === "logs" ? " active" : ""}`} id="view-logs" style={sidecarStatus === "offline" ? { display: "none" } : undefined}>
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
          border: `1px solid ${(toastType === "warning" || toastType === "warn") ? "var(--warning)" : toastType === "success" ? "var(--success)" : "var(--border-strong)"}`,
          borderLeft: `3px solid ${(toastType === "warning" || toastType === "warn") ? "var(--warning)" : toastType === "success" ? "var(--success)" : "var(--accent)"}`,
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
