import { useState, useRef, useEffect, useCallback } from "react";
import { fetch } from "@tauri-apps/plugin-http";
import { invoke } from "@tauri-apps/api/core";
import logoUrl from "./assets/logo.png";
import type { Message, PendingFile, SessionInfo, ViewId, CloudModel } from "./types";
import { saveSessionsWithFallback } from "./utils/storage";
import { needsEngineLoad } from "./utils/modelSelection";
import { localVoicesForLang, speechSupported, voicesForLang } from "./utils/speech";
// API keys stored in OS keychain via Rust commands (store_secret/get_secret/delete_secret)
import { useSessions } from "./hooks/useSessions";
import { useLocalLlm } from "./hooks/useLocalLlm";
import { useChatStream } from "./hooks/useChatStream";
import { useTts } from "./hooks/useTts";
import { sidecarFetch, waitForSidecar, authFetch, uploadSidecarFile, uploadLocalPath } from "./utils/api";
import { useTranslation } from "./i18n";
import { useCronJobs } from "./hooks/useCronJobs";
import ChatView from "./components/ChatView";
import PreviewPanel from "./components/PreviewPanel";
import type { PreviewItem } from "./components/PreviewPanel";
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
      const preview = raw.length > 1000 ? raw.slice(0, 1000) + "\n...(truncated)" : raw;
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
    session, messages, setSelectedModel, setMessages, setMessagesFor, newSession, syncRemote } = useSessions();
  // 停止按钮服务端取消用（P0 修复）：会话 id 经 ref 取最新值，避免闭包过期
  const sessionIdRef = useRef<string>("");
  sessionIdRef.current = session.id;
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

  // 朗读（TTS）：开关 / 语速 / 音色 / 当前正在朗读的消息
  // 第一轮「框架先行」：先问本地语音服务（/v1/synthesize_speech），服务没装或没起来
  // 就直接回退系统语音 —— 所以现在听到的是系统音，装上服务后自动升级，前端一行不用改。
  const [ttsEnabled, setTtsEnabled] = useState<boolean>(
    () => (localStorage.getItem("latiao_tts") ?? "true") !== "false"
  );
  useEffect(() => { localStorage.setItem("latiao_tts", String(ttsEnabled)); }, [ttsEnabled]);
  const [ttsRate, setTtsRate] = useState<number>(
    () => Number(localStorage.getItem("latiao_tts_rate")) || 1
  );
  useEffect(() => { localStorage.setItem("latiao_tts_rate", String(ttsRate)); }, [ttsRate]);
  const [ttsVoice, setTtsVoice] = useState<string>(() => localStorage.getItem("latiao_tts_voice") || "");
  useEffect(() => { localStorage.setItem("latiao_tts_voice", ttsVoice); }, [ttsVoice]);
  const [ttsVoices, setTtsVoices] = useState<SpeechSynthesisVoice[]>([]);
  // 合成等待上限：默认 8s（系统语音/小模型够用），由 /v1/tts/status 按配置覆盖
  const [ttsTimeoutMs, setTtsTimeoutMs] = useState(8000);
  // 本地语音服务的音色 id（与系统语音列表是两套命名，不能混用）
  const [ttsLocalVoices, setTtsLocalVoices] = useState<string[]>([]);
  // 音色 → 语言（服务给的 details）：按界面语言只列该语言的音色
  const [ttsLocalVoiceLangs, setTtsLocalVoiceLangs] = useState<Record<string, string>>({});
  const [ttsPitch, setTtsPitch] = useState<number>(() => Number(localStorage.getItem("latiao_tts_pitch")) || 1);
  const [ttsEmotion, setTtsEmotion] = useState<string>(() => localStorage.getItem("latiao_tts_emotion") || "");
  const [ttsAutoRead, setTtsAutoRead] = useState<boolean>(() => localStorage.getItem("latiao_tts_autoread") === "1");
  // 「本地音色」下拉只列当前界面语言的音色（157 个混在一起没法挑）；
  // 语言判不出来（""）的一律显示，筛空了也回落全表 —— 不能把音色藏没。
  const [ttsLocalVoice, setTtsLocalVoice] = useState<string>(
    () => localStorage.getItem("latiao_tts_local_voice") || ""
  );

  const localVoicesShown = localVoicesForLang(ttsLocalVoices, ttsLocalVoiceLangs, lang, ttsLocalVoice);

  // 权限模式五档（从保守到放手）：read_only / confirm / auto_edit / plan / full
  // 思考强度三档（🧠 选择器）：off / high(默认) / max
  const [thinkingLevel, setThinkingLevel] = useState<"off" | "low" | "high" | "max">(
    () => (localStorage.getItem("latiao_thinking") as "off" | "low" | "high" | "max") || "high"
  );
  useEffect(() => { localStorage.setItem("latiao_thinking", thinkingLevel); }, [thinkingLevel]);

  const [accessMode, setAccessMode] = useState<"read_only" | "confirm" | "auto_edit" | "plan" | "full">(() => {
    const saved = localStorage.getItem("latiao_access");
    if (!saved) {
      // 迁移旧版独立"计划模式"开关
      try { if (JSON.parse(localStorage.getItem("local_ai_os_plan_mode") || "false")) return "plan"; } catch { /* ignore */ }
      // 默认 confirm（与后端 api_routes/agent_loop 默认值一致）：高风险操作每次确认
      return "confirm";
    }
    // 旧版本值迁移：workspace → auto_edit
    if (saved === "workspace") return "auto_edit";
    return (["read_only", "confirm", "auto_edit", "plan", "full"].includes(saved) ? saved : "confirm") as "read_only" | "confirm" | "auto_edit" | "plan" | "full";
  });
  useEffect(() => { localStorage.setItem("latiao_access", accessMode); }, [accessMode]);


  const [prompt, setPrompt] = useState("");
  // 处理中状态**按会话**记录（09-23）：此前是一个全局布尔——A 会话在等回复时
  // B 会话的发送键被判定为"处理中"，于是否决发送/被当成"打断"，表现为
  // "一个会话在跑，另一个会话发不出去"。现在是 Record<sessionId, true>，
  // 每个会话只看自己那一位；两个会话可以各自跑各自的一轮。
  const [processing, setProcessing] = useState<Record<string, boolean>>({});
  // 当前**查看的**会话是否在跑（渲染/发送/停止都只看这一位）
  const isProcessing = !!processing[session.id];
  // 当前执行中任务摘要（tool_start/tool_end 驱动，顶部状态条展示）
  const [activeTask, setActiveTask] = useState<string | null>(null);
  // 后台子智能体任务（ZCode 式活动栏：delegate_task background=true 产生）
  const [subagents, setSubagents] = useState<{ id: string; agent: string; task: string; status: string; steps?: number; activity?: Record<string, number>; last_activity?: string; summary?: string }[]>([]);
  const [taskStartAt, setTaskStartAt] = useState<number | null>(null);
  // 流式中的思考缓冲（运行中 Think 行实时摘要；800ms 节流，见 flushStream）
  const [streamingThink, setStreamingThink] = useState<string>("");
  // 任务栈与中止控制器**按会话**存：A 在跑时切到 B，B 的工具栈/停止键不能
  // 受 A 影响，A 的流也不能被 B 的停止键打断（此前都是全局单值）
  // 回合令牌（每个会话一个自增值）：旧流收尾时校验自己是否仍是该会话的当前回合，
  // 不是就不动状态（打断后立刻重发/两会话并发的状态互相踩）
  const turnSeqRef = useRef(0);
  const turnIdsRef = useRef<Record<string, number>>({});
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
  const [previewItem, setPreviewItem] = useState<PreviewItem | null>(null);
  const [isRecording, setIsRecording] = useState(false);
  const [cloudModels, setCloudModels] = useState<CloudModel[]>([]);
  const [cloudModelsLoaded, setCloudModelsLoaded] = useState(false);

  // Load cloud models from sidecar config（密钥代理化：只回 has_key，明文不进 webview）
  // 必须拿到一次成功 GET 才置 loaded——否则冷启动 GET 失败 → 空表落盘 effect
  // 会把 config.json 里的云端模型连 key 整表清掉（2026-09-24 审计 P0-1）。
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const { sidecarFetchWithRetry } = await import("./utils/api");
        const data = await sidecarFetchWithRetry("/v1/settings/cloud-models", "GET", undefined, 5);
        if (!alive) return;
        if (data?.status === "ok" && Array.isArray(data.models)) {
          setCloudModels((data.models as { name: string; endpoint: string; protocol?: string; has_key?: boolean; max_tokens?: number }[]).map((m) => ({
            name: m.name, endpoint: m.endpoint, protocol: m.protocol || "openai",
            has_key: !!m.has_key, key: "", max_tokens: m.max_tokens || 32768,
          })));
          setCloudModelsLoaded(true);
        }
      } catch { /* 未成功加载则保持 loaded=false，禁止空表回写 */ }
    })();
    return () => { alive = false; };
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
  // Windows 无 macOS 的系统磨砂材质（sidebar/hudWindow 为 macOS 专属），
  // 透明窗会直接透出桌面（09-14 事故）。此处标记平台，CSS 据此用不透明底色。
  useEffect(() => {
    try {
      if (navigator.userAgent.includes("Windows")) {
        document.documentElement.setAttribute("data-platform", "win");
      }
    } catch { /* 忽略 */ }
  }, []);
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    try { return (localStorage.getItem("latiao_theme") as "light" | "dark") || "dark"; }
    catch (e) { console.error(e); return "dark"; }
  });
  // 原生窗口外观跟随主题（磨砂材质随之切换）：浅色切 Light，深色显式设回 Dark
  // （tauri.conf 静态 Dark 之外的动态补充，深色启动路径不变）
  useEffect(() => {
    import("@tauri-apps/api/window").then(({ getCurrentWindow }) => {
      getCurrentWindow().setTheme(theme === "light" ? "light" : "dark").catch(() => {});
    });
  }, [theme]);
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
  const [routeInfo, setRouteInfo] = useState<{ engine: string; declaredModel: string } | null>(null);
  const [activeAgent, setActiveAgent] = useState<string>("latiao");
  const [capabilities, setCapabilities] = useState<Capability[]>([]);

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
  // 朗读：正在朗读的消息 id（再点一次＝停）+ 本地语音服务返回的音频元素
  /* ── Persistence: debounce during SSE streaming, immediate otherwise ── */
  // 同步镜像（state 更新滞后于渲染，同 tick 的双击/回车必须靠 ref 判断）
  const processingRef = useRef<Record<string, boolean>>({});
  processingRef.current = processing;
  // 任一会话在跑 → 落盘节流（别的会话在流式时也没必要每 120ms 全量写 localStorage）
  const anyProcessing = Object.keys(processing).length > 0;
  // 最新 pendingFile 镜像 + 后台英化完成等待者（sendMessage 组装消息前
  // 若英化未完成，等待英化结果拿英文内容——用户偏好"上传文字以英文上传"）
  const pendingFileRef = useRef<PendingFile | null>(null);
  const enWaitersRef = useRef<Set<() => void>>(new Set());
  useEffect(() => { pendingFileRef.current = pendingFile; }, [pendingFile]);

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
    // 审计 P0-5：配额降级逻辑抽为纯函数（storage.ts），行为与原实现一致且可测
    saveSessionsWithFallback(data);
  }, []);

  useEffect(() => {
    const stripped = stripForStorage(sessions);
    const data = JSON.stringify(stripped);
    // ⑩：后端为权威存储（localStorage 保留为降级路径）。syncRemote 内部按会话
    // diff + 每会话防抖，流式期间不会每个 token 都发一次。
    syncRemote(stripped);
    if (anyProcessing) {
      // Streaming: debounce to 1s to avoid thrashing
      const timer = setTimeout(() => saveSessions(data), 1000);
      return () => clearTimeout(timer);
    } else {
      // Not streaming: save immediately
      saveSessions(data);
    }
  }, [sessions, anyProcessing, stripForStorage, saveSessions, syncRemote]);

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
  }, [currentIdx, session.id]);
  // Persist cloud models to OS keychain (debounced to avoid writes on every keystroke)
  useEffect(() => {
    if (!cloudModelsLoaded) return;
    // 防空表误清空：只有加载成功后的用户改动才落盘。空表 + 从未成功加载 = 禁止。
    const timer = setTimeout(async () => {
      try {
        // 只写 sidecar config（空 key 由服务端沿用已存密钥）。不再整包写 keychain，
        // 避免把全部 API key 以可读 JSON 再复制一份给 get_secret 回读。
        const { sidecarFetchWithRetry } = await import("./utils/api");
        // 空表 = 用户删光了模型，必须显式 clear（后端拒绝无 clear 的整表清空）
        await sidecarFetchWithRetry("/v1/settings/cloud-models", "POST", {
          models: cloudModels,
          clear: cloudModels.length === 0,
        }, 3);
      } catch (e) { console.warn("Failed to persist cloud models", e); }
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


  // 初值留空：t 在这里还没定义（useTranslation 在后面），马上会被状态更新填上
  const [fetchDiag, setFetchDiag] = useState("");


  // Fetch capabilities (统一能力模型：工具+技能) from sidecar (via Rust IPC proxy) with health check + retry
  const fetchCapabilities = async () => {
    setFetchDiag(t("app.sidecar_checking"));
    const healthy = await waitForSidecar();
    if (!healthy) {
      setFetchDiag(t("app.sidecar_down", { url: SIDECAR }));
      return;
    }

    const maxRetries = 5;
    for (let attempt = 0; attempt < maxRetries; attempt++) {
      try {
        if (attempt > 0) {
          setFetchDiag(t("app.sidecar_retry", { n: attempt, max: maxRetries }));
          await new Promise(r => setTimeout(r, 2000));
        } else {
          setFetchDiag(t("app.sidecar_caps"));
        }
        const data = await sidecarFetch("/v1/capabilities");
        setFetchDiag(`✅ /v1/capabilities → status=${data.status}`);
        if (data.status === "ok") {
          setCapabilities(data.capabilities || []);
          setFetchDiag(d => `${d}, capabilities=${data.capabilities?.length || 0}`);
        }
        return; // success
      } catch (e: unknown) {
        if (attempt === maxRetries - 1) {
          setFetchDiag(t("app.sidecar_caps_error", { msg: (e instanceof Error ? e.message : String(e)), n: maxRetries }));
        }
      }
    }
  };
  // 启动拉一次能力表即可；t 随语言变化不值得重拉
  // eslint-disable-next-line react-hooks/exhaustive-deps
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
            showToast(t("app.sidecar_restarted"));
            return;
          }
        } catch { /* still starting */ }
      }
      showToast(t("app.sidecar_restart_dead"));
    } catch (e: unknown) {
      showToast(t("app.sidecar_restart_fail", { msg: (e instanceof Error ? e.message : String(e)) }));
    } finally {
      setRestartingSidecar(false);
    }
  };

  // Fetch context estimate
  const switchSession = (idx: number) => {
    setCurrentIdx(idx);
    setPendingFile(null);
    setActiveView("chat");
    // 活动栏/任务条先清空：下一个心跳会填回**该会话**自己的子任务
    setSubagents([]);
    // 切回会话时恢复**它自己**的阶段/任务显示（09-23 按会话隔离）：
    // A 在后台跑、切到 B 再切回 A，顶部状态条要还是 A 的进度而不是 B 的残留
    const target = sessions[idx];
    if (target) {
      const stack = taskStacksRef.current[target.id] || [];
      setActiveTask(stack[stack.length - 1] || null);
      setTaskStartAt(taskStartsRef.current[target.id] || null);
      setStreamingThink("");
      setAgentPhase(processingRef.current[target.id] ? t("agent.phase_analyze") : "");
    }
  };
  const deleteSession = (idx: number) => {
    // 复用 hook 的 newSession()（crypto.randomUUID）：此前这里另起一套
    // Math.random().substring(7) 低熵 id（约 20-30 bit）——会话 id 是消息写入、
    // 取消请求、进度文件的定位键，碰撞即跨会话串话（审计 A2）
    setSessions((prev) => {
      const next = prev.filter((_, i) => i !== idx);
      if (next.length === 0) return [newSession()];
      return next;
    });
    // 删掉的是正在查看的会话时，把查看位置挪到存在的会话上
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


  // Unified heartbeat: sidecar status + downloads + learnings
  useEffect(() => {
    const tick = async () => {
      // Unified sidecar heartbeat
      try {
        // session_id：活动栏只显示本会话的子代理（此前混着所有会话的任务）
        const resp = await authFetch(`/v1/heartbeat?session_id=${encodeURIComponent(sessionIdRef.current)}`,
                                     { signal: AbortSignal.timeout(5000) });
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
              const content = `**${header}**\n\n${(ev.full || ev.summary || "").trim() || t("app.no_output")}`;
              const s = newSession();
              const name = `⏰ ${ev.task
                  // emoji 要带 u 标志并用码点写：不加 u 时字符类匹配的是**代理半区**，
                  // 会把别的 emoji（与这五个共享半区的，如 🔎🚀）削掉一半变成非法字符
                  .replace(/[\u{1F50D}\u{1F4CB}\u{1F4CA}\u{26A1}\u{1F4C8}]|\s*\(记录到记忆库\)/gu, "")
                  .trim().slice(0, 20)} ${ev.ts.slice(5, 16).replace("T", " ")}`;
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
    // 5s 心跳只挂一次：t/showToast 等变化不重置 interval（会漏检/双跑）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const {
    localModelId, setLocalModelId, setupCheck, fixing, runFix,
    hfSearch, setHfSearch, hfResults, searching, searchHF,
    downloadProgress, downloadModel, pauseDownload, resumeDownload, cancelDownload,
    contextLimit, contextEstimate, fetchContextEstimate, updateContextLimit,
    localLLMStatus, setLocalLLMStatus, startLocalLLM, stopLocalLLM,
  } = useLocalLlm({ showToast, t, setSelectedModel });

  const {
    streamChat, confirmTool, stopGeneration,
    taskStacksRef, taskStartsRef, abortControllersRef,
  } = useChatStream({
    t, showToast, cloudModels, session, sessionIdRef,
    setMessages, setMessagesFor, setStreamingThink, setAgentPhase,
    setActiveTask, setTaskStartAt, setRouteInfo, setProcessing, processingRef,
    reflectionMode, accessMode, thinkingLevel,
  });



  const { speakingId, speak } = useTts({
    showToast, t, ttsEnabled, ttsRate, ttsVoice, ttsTimeoutMs,
    ttsLocalVoices, ttsLocalVoice, ttsPitch, ttsEmotion, ttsVoices, lang, session,
  });



  // 选模型 = 顺带加载（2026-09-23 用户要求）：下拉选中的与会话标签是两回事，
  // 只改标签不切换引擎 → 实际还是旧模型在回答（"对话框显示的模型和引擎里装的不一样"）。
  // 本地模型且与已加载不同才真加载；云端/空值不动引擎；引擎忙由 startLocalLLM 拒绝并提示。
  const selectModelAndLoad = useCallback(async (m: string) => {
    setSelectedModel(m);
    if (!needsEngineLoad(m, cloudModels, localLLMStatus)) return;
    const _short = String(m).split("/").filter(Boolean).pop() || m;
    showToast(t("toast.model_switching", { model: _short }));
    await startLocalLLM(m);
  }, [cloudModels, localLLMStatus, setSelectedModel, showToast, t, startLocalLLM]);


  /* ── 自动更新（此前零接线：死开关 + 硬编码版本号，审计修复）── */
  const [appVersion, setAppVersion] = useState("…");
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const runUpdateCheck = useCallback(async (silent: boolean) => {
    if (checkingUpdate) {
      if (!silent) showToast(t("app.update_busy"), "info");
      return;
    }
    setCheckingUpdate(true);
    const { checkForUpdates } = await import("./utils/updater");
    // updater 只给键与参数（它不该依赖 i18n），这里翻成当前语言
    const res = await checkForUpdates((key, params) => showToast(t(key, params)), !silent);
    setCheckingUpdate(false);
    if (!silent && res === "none") showToast(t("update.uptodate"));
  }, [checkingUpdate, showToast, t]);
  useEffect(() => {
    import("./utils/updater").then(({ getAppVersion }) => {
      getAppVersion().then(setAppVersion).catch(() => setAppVersion("…"));
    }).catch(() => setAppVersion("…"));
    if (autoCheckUpdate) runUpdateCheck(true);
    // 挂载时查一次版本/更新；runUpdateCheck 内部自管 checkingUpdate
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Extracted hooks (cron — depends on showToast)
  const { cronJobs, newCron, setNewCron, addCronJob, toggleCronJob, deleteCronJob, runCronJob } = useCronJobs(showToast);

  /* ── Stream Chat (preserved from original) ── */
  const sendMessage = async () => {
    const text = prompt;
    if (!text.trim() && !pendingFile) return;
    // Re-entrancy guard: if a previous request is still streaming, ignore
    // duplicate sends (double-click, repeated Enter, etc.). Without this,
    // concurrent agent loops pile up (msg_count doubles each send), the
    // local model server gets hammered by parallel requests + retry storms,
    // and the sidecar eventually crashes -> "Unhandled Promise Rejection".
    // ⚠️ 必须用 ref 同步判断：isProcessing state 更新滞后于渲染，同 tick
    // 内两次 Enter/双击都在 state 更新前通过 → 双循环（审计 P1）
    // 09-23：只看**本会话**是否在跑——别的会话在跑与我无关（此前全局布尔导致
    // "另一个会话在等回复时，这个会话发不出去"）
    if (processingRef.current[session.id]) {
      // 本会话任务执行中发送新消息 = 中止当前任务再发（用户可随时插话/纠正方向；
      // 后端取消由 stopGeneration 同步触发 /v1/chat/cancel，循环不残留）
      stopGeneration(session.id);
    }
    // 本轮的令牌：本轮 finally 只有在"令牌仍是自己的"时才清处理中标记，
    // 避免"打断后立刻重发"时旧流的收尾把新流的标记清掉（红停止键永不恢复）
    const myTurn = ++turnSeqRef.current;
    turnIdsRef.current[session.id] = myTurn;
    processingRef.current[session.id] = true;

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
    setProcessing((p) => ({ ...p, [session.id]: true }));
    setAgentPhase(t("agent.phase_analyze"));
    taskStacksRef.current[session.id] = []; // 清残留工具栈（上次中断/未正常结束）
    taskStartsRef.current[session.id] = Date.now(); // 任务头部"已工作"计时起点
    setActiveTask(null);
    setTaskStartAt(taskStartsRef.current[session.id]);

    // 等待后台英化完成再组装消息（用户偏好：上传文字以英文上传）。
    // 12 秒上限：超时/失败用当前内容兜底，不阻塞发送。
    let pf = pendingFileRef.current || pendingFile;
    if (pf && pf.type === "file" && !pf.enReady) {
      // 英化等待改为静默（09-21 用户反馈：不要弹窗；日志在 sidecar 侧可见）
      await new Promise<void>((resolve) => {
        const timer = setTimeout(() => resolve(), 12000);
        enWaitersRef.current.add(() => { clearTimeout(timer); resolve(); });
      });
      pf = pendingFileRef.current || pf;
    }

    const userMsg: Message = { id: msgId(), role: "user", content: text || "Analyze this file", ts: Date.now() };
    if (pf) {
      userMsg.type = pf.type === "image" ? "image" : "file";
      userMsg.filename = pf.name;
      if (pf.base64) {
        userMsg.imageBase64 = pf.base64;
        userMsg.imageMime = pf.mimeType;
        // imagePreview drives the in-bubble thumbnail (ChatView.tsx); without it
        // the screenshot history collapses to a "[File: ...]" text line.
        if (pf.type === "image" && pf.preview) userMsg.imagePreview = pf.preview;
      }
      if (pf.type === "image") {
        userMsg.content = text || `[Image: ${pf.name}]`;
      } else {
        // 文本附件必须把内容真正发给模型（此前只发占位符，模型看不到文件）。
        // 太长截断：上下文有限，128KB 足够覆盖绝大多数源码/文档。
        const MAX_FILE_CHARS = 128 * 1024;
        let body = (pf.content || "").slice(0, MAX_FILE_CHARS);
        if ((pf.content || "").length > MAX_FILE_CHARS) body += "\n\n" + t("app.file_truncated");
        userMsg.content =
          (text ? text + "\n\n" : "") +
          `${t("app.attached_file", { name: pf.name })}\n\`\`\`\n${body}\n\`\`\``;
      }
    }

    // 发送即清预览框（此前只在 finally 清：请求期间预览残留；且任务执行中
    // 发送会先经 stopGeneration 把 pendingFile 提前清掉导致文件丢失）
    setPendingFile(null);
    setMessagesFor(session.id, (prev) => [...prev, userMsg]);

    const assistantPlaceholder: Message = { id: msgId(), role: "assistant", content: "", ts: Date.now() };
    setMessagesFor(session.id, (prev) => [...prev, assistantPlaceholder]);

    try {
      const apiMessages = buildApiMessages(session, userMsg, accessMode === "plan", lang);
      const opts: Record<string, unknown> = { agent: activeAgent, sessionId: session.id };
      if (session.selectedModel) opts.model = session.selectedModel;
      if (cloudCfgPre) opts.cloudConfig = { key: cloudCfgPre.key, endpoint: cloudCfgPre.endpoint, protocol: cloudCfgPre.protocol || "openai" };

      const controller = new AbortController();
      abortControllersRef.current[session.id] = controller;
      const finalText = await streamChat(apiMessages, opts, controller.signal);
      // 自动朗读：只在整轮成功结束时念一次正文。报错（❌ 开头）和用户中止的都不念。
      if (ttsAutoRead && finalText && finalText.trim() && !finalText.trimStart().startsWith("❌")) {
        void speak(finalText, assistantPlaceholder.id);
      }
    } catch (e) {
      // Tauri plugin-http teardown after abort rejects with "The resource id
      // ... is invalid" instead of a standard AbortError — treat it the same.
      const errText = String((e as { message?: string })?.message ?? e ?? "");
      const aborted = (e as { name?: string })?.name === "AbortError"
        || /resource id .* is invalid/i.test(errText);
      setMessagesFor(session.id, (prev) => {
        const msgs = [...prev];
        const last = msgs[msgs.length - 1];
        if (last?.role === "assistant") {
          if (aborted) {
            // User pressed Stop: keep whatever was already generated instead of
            // overwriting it with an error. Drop only a still-empty placeholder.
            if (last.content) msgs[msgs.length - 1] = { ...last, content: `${last.content}\n\n${t("app.stopped_suffix")}` };
            else msgs.pop();
          } else {
            msgs[msgs.length - 1] = { ...last, content: `❌ ${e}` };
          }
        }
        return msgs;
      });
    } finally {
      abortControllersRef.current[session.id] = null;
      // 只有"令牌还是自己"才清处理中标记（见上面 myTurn）：打断后立刻重发时，
      // 旧流的收尾不能把新回合的标记清掉（红停止键永不恢复的旧根因）
      if (turnIdsRef.current[session.id] === myTurn) {
        delete turnIdsRef.current[session.id];
        delete processingRef.current[session.id];
        setProcessing((p) => { const n = { ...p }; delete n[session.id]; return n; });
        if (sessionIdRef.current === session.id) {
          setStreamingThink("");
          setAgentPhase("");
          setActiveTask(null);
          setTaskStartAt(null);
        }
      }
      setPendingFile(null);
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

  const lastDropRef = useRef<{ path: string; ts: number }>({ path: "", ts: 0 });

  const handleDrop = useCallback(async (e: React.DragEvent) => {
    e.preventDefault();
    // HTML5 拖放（保底路径）：多数情况下 Tauri 原生 onDragDropEvent 已处理
    const file = e.dataTransfer?.files?.[0];
    if (!file) return;
    if (file.type.startsWith("image/")) {
      await processImageFile(file);
    } else {
      try {
        const data = await uploadSidecarFile(file);
        if (data?.status === "success" && data.content) {
          setPendingFile({ name: file.name, preview: "📄", type: "file", content: String(data.content) });
          if (file.type === "application/pdf") showToast(t("app.pdf_extracted"));
        } else {
          showToast(String(data?.message || t("app.file_parse_fail")), "warn");
        }
      } catch {
        showToast(t("app.file_upload_fail"), "warn");
      }
    }
    // 拖放 handler 挂一次即可；t/processImageFile 变化不重绑
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Tauri 原生拖放（macOS 稳定路径）：onDragDropEvent 拿到文件路径，sidecar 读盘
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    (async () => {
      try {
        // 双监听兜底：webview 与 webviewWindow 两个模块的事件源在不同
        // Tauri 版本/平台上行为不一致（#14055），两个都挂，共享 handler；
        // 防抖（tauri#14134 macOS 重复触发）确保同一路径 1.5s 内只处理一次。
        const handler = async (event: { payload?: { type?: string; paths?: string[] } }) => {
          const payload = event?.payload;
          if (!payload || payload.type !== "drop") return;
          const path = (payload.paths || [])[0];
          if (!path) return;
          const now = Date.now();
          if (lastDropRef.current.path === path && now - lastDropRef.current.ts < 1500) return;
          lastDropRef.current = { path, ts: now };
          // 预览秒出：先显示"解析中"，后端解析+英化完成后更新内容
          //（翻译是云端调用，大文件可达 1-2 分钟——预览框不能等它）
          const name0 = path.split("/").pop() || t("app.file_word");
          setPendingFile({ name: name0, preview: "📄", type: "file", content: t("app.parsing_file") });
          try {
            // 第一段：translate=false 秒回解析原文，预览立即显示真实内容；
            // 第二段：后台再请求 translate=true 的英文版，完成后替换预览。
            const data = await uploadLocalPath(path, false);
            if (data?.status !== "success") {
              setPendingFile(null);
              showToast(String((data as { message?: string })?.message || t("app.file_parse_fail")), "warn");
              return;
            }
            const d = data as { content?: string; base64_data?: string; content_type?: string; filename?: string; is_image?: boolean };
            const name = d.filename || path.split("/").pop() || t("app.file_word");
            if (d.is_image === true || (d.base64_data && (d.content_type || "").startsWith("image/"))) {
              const mime = d.content_type || "image/png";
              setPendingFile({ name, preview: `data:${mime};base64,${d.base64_data}`, type: "image", content: `data:${mime};base64,${d.base64_data}`, base64: d.base64_data, mimeType: mime });
            } else {
              setPendingFile({ name, preview: "📄", type: "file", content: String(d.content || "") });
              if ((d.content_type || "").includes("pdf") || name.toLowerCase().endsWith(".pdf")) showToast(t("app.pdf_extracted"));
              // 后台英化更新（不阻塞发送——发送时用当前已有内容）
              uploadLocalPath(path, true).then((td) => {
                setPendingFile((prev) => prev && prev.name === name
                  ? (td?.status === "success" && td.content
                      ? { ...prev, content: String(td.content), enReady: true }
                      : { ...prev, enReady: true })  // 失败也标记完成：保留原文可发送
                  : prev);
                enWaitersRef.current.forEach((w) => w());
                enWaitersRef.current.clear();
              }).catch(() => {
                setPendingFile((prev) => prev && prev.name === name ? { ...prev, enReady: true } : prev);
                enWaitersRef.current.forEach((w) => w());
                enWaitersRef.current.clear();
              });
            }
          } catch {
            showToast(t("app.file_upload_fail"), "warn");
          }
        };
        const unlisteners: (() => void)[] = [];
        try {
          const { getCurrentWebviewWindow } = await import("@tauri-apps/api/webviewWindow");
          unlisteners.push(await getCurrentWebviewWindow().onDragDropEvent(handler));
        } catch (e) {
          console.warn("[drag-drop] webviewWindow 监听失败", e);
        }
        try {
          const { getCurrentWebview } = await import("@tauri-apps/api/webview");
          unlisteners.push(await getCurrentWebview().onDragDropEvent(handler));
        } catch (e) {
          console.warn("[drag-drop] webview 监听失败", e);
        }
        unlisten = () => unlisteners.forEach((u) => { try { u(); } catch { /* noop */ } });
        console.info("[drag-drop] 原生拖放监听已挂载（双通道）");
        // 去掉启动 toast（09-21 用户反馈：状态类提示不进右下角弹窗）
      } catch (e) {
        console.error("[drag-drop] 挂载失败", e);
        showToast(t("app.drop_init_fail", { msg: String((e as { message?: string })?.message || e) }), "warn");
      }
    })();
    return () => { unlisten?.(); };
    // 双通道原生监听挂一次；t 变化不重绑（否则每次切语言都 unlisten/relisten）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showToast]);

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.type.startsWith("image/")) {
      await processImageFile(file);
    } else {
      // PDF/文本等统一走后端 /v1/upload_file：PDF 提取文字、文本英化
      // （用户偏好 upload_text_en），本地 FileReader 会绕过英化。
      try {
        const data = await uploadSidecarFile(file);
        if (data?.status === "success" && data.content) {
          setPendingFile({ name: file.name, preview: "📄", type: "file", content: String(data.content) });
          if (file.type === "application/pdf") showToast(t("app.pdf_extracted"));
        } else {
          showToast(String(data?.message || t("app.file_parse_fail")), "warn");
        }
      } catch {
        showToast(t("app.file_upload_fail"), "warn");
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

  /* ── 朗读（TTS）：本地语音服务优先，系统语音兜底 ── */
  useEffect(() => {
    if (!speechSupported()) return;
    const load = () => {
      try { setTtsVoices(window.speechSynthesis.getVoices() || []); } catch { /* ignore */ }
    };
    load();  // 有的 WebView 首次为空，靠 voiceschanged 再补一次
    try { window.speechSynthesis.addEventListener("voiceschanged", load); } catch { /* ignore */ }
    return () => {
      try { window.speechSynthesis.removeEventListener("voiceschanged", load); } catch { /* ignore */ }
    };
  }, []);

  // 合成超时跟着服务端配置走（默认仍是 8s）。本机大模型要 6~20s，靠它一处配置调开，
  // 前端不用改代码；服务不可用由 sidecar 的探测先挡掉，所以放长不会白等。
  // 顺带取本地语音服务的音色表：设置页那份是**系统语音**的名字，本地服务不认，
  // 拿它去合成会 500（audio.cpp: unknown voice id）→ 对不上就不发，让服务用默认音色。
  const onSettingsPage = activeView === "settings";
  useEffect(() => {
    if (!ttsEnabled) return;
    let alive = true;
    (async () => {
      try {
        const resp = await authFetch("/v1/tts/status", { signal: AbortSignal.timeout(4000) });
        if (resp.ok) {
          const data = await resp.json();
          const secs = Number(data?.timeout);
          if (alive && Number.isFinite(secs) && secs > 0) setTtsTimeoutMs(Math.round(secs * 1000));
          // 用户没手动设过滑杆时，采用服务端配置的音高（否则滑杆和服务端各说各话）
          const svcPitch = Number(data?.pitch);
          if (alive && Number.isFinite(svcPitch) && svcPitch > 0
              && localStorage.getItem("latiao_tts_pitch") === null) {
            setTtsPitch(svcPitch);
          }
        }
      } catch { /* 拿不到就用默认 8s */ }
      try {
        const resp = await authFetch("/v1/tts/voices", { signal: AbortSignal.timeout(4000) });
        if (resp.ok) {
          const data = await resp.json();
          const list = Array.isArray(data?.voices) ? data.voices : [];
          const details = Array.isArray(data?.details) ? data.details : [];
          if (alive) {
            setTtsLocalVoices(list.map((v: unknown) => String(v)));
            const map: Record<string, string> = {};
            for (const d of details) {
              const name = String((d as { name?: unknown })?.name ?? "");
              if (name) map[name] = String((d as { lang?: unknown })?.lang ?? "");
            }
            setTtsLocalVoiceLangs(map);
          }
        }
      } catch { /* 服务不在 → 空表，走系统语音 */ }
    })();
    return () => { alive = false; };
    // 依赖里带上「是否停在设置页」：音色列表原来只在开启朗读那一刻抓一次，
    // 若那次服务恰好没起来（比如刚开机、服务正在重启），列表会一直空着 ——
    // 用户看到的就是"音色没了"。现在每次打开设置页都会重抓一次。
  }, [ttsEnabled, onSettingsPage]);

  // 抓空了就过几秒自己再试一次（服务刚起来/正在重启时最有用），最多试 3 轮
  useEffect(() => {
    if (!ttsEnabled || ttsLocalVoices.length > 0) return;
    let tries = 0;
    const timer = setInterval(async () => {
      tries += 1;
      if (tries > 3) { clearInterval(timer); return; }
      try {
        const resp = await authFetch("/v1/tts/voices", { signal: AbortSignal.timeout(4000) });
        if (!resp.ok) return;
        const data = await resp.json();
        const list = Array.isArray(data?.voices) ? data.voices : [];
        if (list.length) {
          setTtsLocalVoices(list.map((v: unknown) => String(v)));
          const map: Record<string, string> = {};
          for (const d of Array.isArray(data?.details) ? data.details : []) {
            const name = String((d as { name?: unknown })?.name ?? "");
            if (name) map[name] = String((d as { lang?: unknown })?.lang ?? "");
          }
          setTtsLocalVoiceLangs(map);
          clearInterval(timer);
        }
      } catch { /* 还没起来，下一轮再试 */ }
    }, 5000);
    return () => clearInterval(timer);
  }, [ttsEnabled, ttsLocalVoices.length]);

  const testConnection = async (modelName: string, _key: string | undefined, endpoint: string, protocol: string) => {
    setTestingModel(modelName);
    setTestResult("");
    try {
      const resp = await authFetch("/v1/test_connection", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: modelName, model: modelName, endpoint, protocol }),
      });
      const data = await resp.json();
      setTestResult(data.status === "ok" ? t("app.conn_ok") : t("app.conn_fail", { msg: data.message || t("app.conn_fail_default") }));
    } catch { setTestResult(t("app.conn_no_sidecar")); }
    finally { setTestingModel(null); }
  };


  /* ═══════════ Render ═══════════ */

  return (
    <div className={`app${sidebarCollapsed ? " sidebar-collapsed" : ""}`} data-theme={theme}>

      {/* ═══ Sidebar ═══ */}
      <aside className="sidebar">
        <div className="sidebar-brand" data-tauri-drag-region>
          {sidebarCollapsed
            ? <img className="sidebar-logo" src={logoUrl} alt="辣条" />
            : <div className="sidebar-logo"><img src={logoUrl} alt="辣条" /></div>}
          {!sidebarCollapsed && <div>
            <div className="sidebar-title">辣条</div>
            <div className="sidebar-subtitle">Latiao</div>
          </div>}
          <button className="sidebar-collapse-btn" onClick={() => setSidebarCollapsed(!sidebarCollapsed)} title={t("sidebar.toggle")}>{sidebarCollapsed ? "☰" : "◁"}</button>
        </div>

        <nav className="sidebar-nav">
          <div className="nav-section-label" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            {!sidebarCollapsed && <span>{t("sidebar.sessions")}</span>}
            <button className="session-add-btn" onClick={() => {
              const ns = newSession();
              // 新会话插到列表顶部（09-08：此前 [...prev, ns] 追加到末尾，
              // 用户在历史会话里找不到新建的会话）
              setSessions((prev) => [ns, ...prev]);
              setCurrentIdx(0);
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
                <div className="session-preview">{(() => {
                // ⑩：后端列表只带元数据，预览由服务端算好（本地已加载时仍可用消息兜底）
                const last = s.messages.length > 0 ? (s.messages[s.messages.length - 1].content || "") : "";
                const src = last || s.preview || "";
                return src ? src.replace(/[#*|`>-]/g, " ").replace(/\s+/g, " ").slice(0, 30) + "..." : t("session.default");
              })()}</div>
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
          <select className="sidebar-model-select" value={session.selectedModel} onChange={(e) => { void selectModelAndLoad(e.target.value); }}>
            <option value="">{t("sidebar.auto_detect")}</option>
            {localLLMStatus.model_id && (
              <option key="local-loaded" value={localLLMStatus.model_id}>💻 {localLLMStatus.model_name || localLLMStatus.model_id.split("/").filter(Boolean).pop()}</option>
            )}
            {cloudModels.map((m) => (<option key={m.name} value={m.name}>☁️ {m.name}</option>))}
          </select>
        </div>
      </aside>

      {/* ═══ Main ═══ */}
      <main className="main" style={{ position: "relative" }}>
        <div className="topbar" data-tauri-drag-region>
          <span className="topbar-title">{session.name.startsWith("session.") ? t(session.name) : session.name}</span>
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
        <div className={`view-row${activeView === "chat" && sidecarStatus !== "offline" ? "" : " hidden-row"}`} style={{ display: "flex", flex: 1, minWidth: 0, minHeight: 0 }}>
        <div className={`view-panel${activeView === "chat" ? " active" : ""}`} id="view-chat" style={sidecarStatus === "offline" ? { display: "none" } : undefined}>
          <ChatView
            messages={messages} isProcessing={isProcessing}
            pendingFile={pendingFile} setPendingFile={setPendingFile}
            prompt={prompt} setPrompt={setPrompt}
            fileInputRef={fileInputRef} mediaRecorderRef={mediaRecorderRef}
            isRecording={isRecording}
            sendMessage={sendMessage} onStop={() => {
              stopGeneration();
              // 立刻把运行中的子智能体收口成「已中断」——后端也会在 /v1/chat/cancel
              // 时同步置终态，但心跳有间隔，这里先改本地避免详情弹窗仍显示「执行中」
              setSubagents(prev => prev.map(s => s.status === "running"
                ? { ...s, status: "error", summary: "已中断（用户停止）" } : s));
            }} handleFileSelect={handleFileSelect}
            onSelectModelAndLoad={selectModelAndLoad} engineStatus={localLLMStatus}
            startRecording={startRecording} confirmTool={confirmTool}
            onSpeak={speak} speakingId={speakingId}
            chatEndRef={chatEndRef} handleDrop={handleDrop}
            onPasteImage={(file) => processImageFile(file, t("app.screenshot", { ts: new Date().toLocaleTimeString() }))}
            cloudModels={cloudModels}
            selectedModel={session.selectedModel}
            accessMode={accessMode} setAccessMode={setAccessMode}
            thinkingLevel={thinkingLevel} setThinkingLevel={setThinkingLevel}
            contextEstimate={contextEstimate}
            sessionId={session.id}
            onPreview={setPreviewItem}
            showToast={showToast}
            activeTask={activeTask}
            taskStartAt={taskStartAt}
            streamingThink={streamingThink}
            subagents={subagents}
            routeInfo={routeInfo}
            localModelId={localLLMStatus.model_id}
            localModelName={localLLMStatus.model_name}
          />
        </div>
          {previewItem && (
            <PreviewPanel item={previewItem} onClose={() => setPreviewItem(null)} />
          )}
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
            active={activeView === "settings"}
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
            ttsEnabled={ttsEnabled} setTtsEnabled={setTtsEnabled}
            ttsRate={ttsRate} setTtsRate={setTtsRate}
            ttsVoice={ttsVoice} setTtsVoice={setTtsVoice}
            ttsVoices={voicesForLang(
              ttsVoices.map(v => ({ name: v.name, lang: v.lang })), lang)}
            ttsLocalVoices={localVoicesShown}
            ttsLocalVoice={ttsLocalVoice}
            setTtsLocalVoice={setTtsLocalVoice}
            ttsPitch={ttsPitch} setTtsPitch={setTtsPitch}
            ttsEmotion={ttsEmotion} setTtsEmotion={setTtsEmotion}
            ttsAutoRead={ttsAutoRead} setTtsAutoRead={setTtsAutoRead}
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
