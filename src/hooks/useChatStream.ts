/** 聊天流 SSE + 工具确认 + 停止。从 App.tsx 拆出（2026-09-24）。 */
import { useCallback, useRef } from "react";
import type { CloudModel, Message } from "../types";
import { parseSSEDataLine } from "../utils/sse";
import { authFetch } from "../utils/api";
import { resolveStopTarget } from "../utils/sessionTarget";


function alwaysAllowKey(sessionId?: string): string {
  // 按会话记「总是允许」——新开聊天不应继承（2026-09-24 用户反馈）
  return sessionId ? `latiao_always_allow:${sessionId}` : "latiao_always_allow:global";
}

function shouldAutoAllow(toolName?: string, sessionId?: string): boolean {
  try {
    const arr: string[] = JSON.parse(localStorage.getItem(alwaysAllowKey(sessionId)) || "[]");
    return !!toolName && arr.includes(toolName);
  } catch { return false; }
}

const msgId = () => `msg_${crypto.randomUUID()}`;

/** 「总是允许」过的工具：自动放行，但必须留下可见痕迹（2026-09-24 用户报「按钮闪现就没了」） */
async function autoAllow(callId: string, toolName?: string, onNote?: (msg: string) => void, sessionId?: string) {
  if (!shouldAutoAllow(toolName, sessionId)) return;
  try {
    await authFetch("/v1/confirm_tool", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ call_id: callId, approved: true }),
    });
    onNote?.(`⚡ ${toolName} 已按「总是允许」自动通过（设置里可清除）`);
  } catch { /* 用户仍可手点 */ }
}

export interface ChatStreamDeps {
  t: (key: string, params?: Record<string, string | number>) => string;
  showToast: (msg: string, type?: string) => void;
  cloudModels: CloudModel[];
  session: { id: string };
  sessionIdRef: { current: string };
  setMessages: (fn: (prev: Message[]) => Message[]) => void;
  setMessagesFor: (id: string, fn: (prev: Message[]) => Message[]) => void;
  setStreamingThink: (v: string) => void;
  setAgentPhase: (v: string) => void;
  setActiveTask: (v: string | null) => void;
  setTaskStartAt: (v: number | null) => void;
  setRouteInfo: (v: { engine: string; declaredModel: string } | null) => void;
  setProcessing: (fn: (p: Record<string, boolean>) => Record<string, boolean>) => void;
  processingRef: { current: Record<string, boolean> };
  reflectionMode: "off" | "light" | "deep";
  accessMode: "read_only" | "confirm" | "auto_edit" | "plan" | "full";
  thinkingLevel: "off" | "low" | "high" | "max";
}

export function useChatStream(deps: ChatStreamDeps) {
  const {
    t, showToast, cloudModels, session, sessionIdRef,
    setMessages, setMessagesFor, setStreamingThink, setAgentPhase,
    setActiveTask, setTaskStartAt, setRouteInfo, setProcessing, processingRef,
    reflectionMode, accessMode, thinkingLevel,
  } = deps;
  const taskStacksRef = useRef<Record<string, string[]>>({});
  const taskStartsRef = useRef<Record<string, number>>({});
  const abortControllersRef = useRef<Record<string, AbortController | null>>({});
  const confirmInFlightRef = useRef<Set<string>>(new Set());
  // streamChat 内部按需使用（见 useChatStream 内 turn 计数）

  const streamChat = async (
    messages: Record<string, unknown>[],
    opts?: { model?: string; agent?: string; cloudConfig?: Record<string, unknown>; skipTools?: boolean; sessionId?: string },
    signal?: AbortSignal,
  ): Promise<string> => {
    // 本轮流的目标会话在**发起时**固化（09-23 并发隔离）：用户中途切到别的
    // 会话时，消息写入仍落到本会话；显示类状态（思考/阶段/任务条）只在
    // "正在看的就是本会话"时更新，避免 A 的流在 B 的界面上闪现（混会话）。
    const targetId = opts?.sessionId || session.id;
    const viewed = () => sessionIdRef.current === targetId;
    const writeMessages = (fn: (prev: Message[]) => Message[]) =>
      (opts?.sessionId ? setMessagesFor(targetId, fn) : setMessages(fn));
    const showThink = (v: string) => { if (viewed()) setStreamingThink(v); };
    const showPhase = (v: string) => { if (viewed()) setAgentPhase(v); };
    const myStack = () => {
      if (!taskStacksRef.current[targetId]) taskStacksRef.current[targetId] = [];
      return taskStacksRef.current[targetId];
    };
    const showStackTop = () => {
      if (viewed()) setActiveTask(taskStacksRef.current[targetId]?.slice(-1)[0] || null);
    };

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
      throw new Error(t("app.sidecar_unreachable", { err: String(e) }), { cause: e });
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
    // 思考起始时间戳：流式期间只攒不发（防"思考闪现多次"），[DONE] 定稿时
    // 一次性附着到助手消息并结算耗时（09-21 用户反馈：思考应只在折叠栏里）。
    let thinkingStartedAt = 0;
    // reflection_revised 已把最终文本写入消息；此后 [DONE]/finally 的 flushStream
    // 若再跑，prefix 检查不匹配会把 revised 文本重复 push 一条（M1 复发）。
    let streamFinalized = false;
    // 定稿后允许最后一次 flush（附着思考），此后不再跑（M1 防重复保护）
    let thinkingAttached = false;
    // 流式期思考实时预览节流（09-21 22:48：本地慢生成前端完全静默——每 ≥10s
    // 写一次"前 120 字…"，既让用户看到"在思考"，又不"闪现内容不同"）
    let lastThinkingFlush = 0;
    let lastThinkPropFlush = 0;
    // 当前 agent 轮次（round_start 事件；09-05 23:52：多轮拉锯黑盒化）
    let currentRound = 0;
    const flushStream = () => {
      if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
      if (streamFinalized && thinkingAttached) return;
      const text = full;
      // 定稿后才附着思考：流式期间正文照常更新，思考攒着不闪现
      const th = streamFinalized ? pendingThinking : "";
      if (streamFinalized) pendingThinking = "";
      // 运行中 Think 行实时摘要（独立轻量 state，800ms 节流——只重渲染
      // 活动行，不打全量消息列表；定稿后清空，行数据交回消息 thinking 字段）
      if (!streamFinalized && pendingThinking) {
        if (Date.now() - lastThinkPropFlush >= 800) {
          lastThinkPropFlush = Date.now();
          showThink(pendingThinking);
        }
      } else if (streamFinalized) {
        showThink("");
      }
      // 非定稿 + 有思考 + 未到节流窗口：跳过空写（避免每 120ms 重渲染）
      const livePreview = !streamFinalized && pendingThinking !== ""
        && Date.now() - lastThinkingFlush >= 10_000;
      if (!text && !th && !livePreview) return;
      writeMessages((prev) => {
        const msgs = [...prev];
        const last = msgs[msgs.length - 1];
        if (last?.role === "assistant") {
          if (text && last.content && !text.startsWith(last.content)) {
            // 已有内容的 assistant（如 📋 执行计划）：正文另起新消息，不覆盖
            msgs.push({ id: msgId(), role: "assistant", content: text, thinking: th || undefined, round: currentRound || undefined, ts: Date.now() });
          } else {
            const updated: Message = { ...last };
            updated.round = currentRound || undefined;
            if (th) {
              updated.thinking = (last.thinking || "") + th;
              // 思考耗时按真实起止结算（附着发生在定稿时）
              updated.thinkingDuration = thinkingStartedAt
                ? Math.max(0, Date.now() - thinkingStartedAt) : undefined;
              thinkingStartedAt = 0;
                        } else if (livePreview) {
              // 09-20 修复：流式期**不再**把预览写进消息的 thinking 字段——
              // 实时行（streamingThink）已经在显示同一段预览，双写会让用户看到
              // 两条一模一样的"前 120 字 + …"（实测截图确认）。这里只更新计时。
              lastThinkingFlush = Date.now();
            }
            if (text) updated.content = text;
            msgs[msgs.length - 1] = updated;
          }
        } else if (text || th || livePreview) {
          // 思考预览也创建消息（正文未到时可先占位；09-21 22:48：仅预览时
          // 无 assistant 消息 → 预览无处挂载，前端 3 分钟无反应）
          msgs.push({
            id: msgId(), role: "assistant", content: text,
            thinking: th || undefined,   // 09-20：预览不再双写（见上）
            round: currentRound || undefined,
            ts: Date.now(),
          });
        }
        return msgs;
      });
    };
    const scheduleFlush = () => {
      if (!flushTimer) flushTimer = setTimeout(flushStream, 0);
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
        if (watchdogFired) throw new Error(t("app.stream_timeout"));
        break;
      }
      armWatchdog();
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        {
          const sse = parseSSEDataLine(line);
          if (sse.kind === "skip") continue;
          if (sse.kind === "done") { streamFinalized = true; flushStream(); thinkingAttached = true; return full; }
          if (sse.kind === "error") throw new Error(sse.message);
          try {
            const parsed = sse.parsed;
            try {
              if (parsed.event === "engine_route") {
                // P0 路由透明化：如实告知用户本请求实际落地的引擎，
                // 避免"选了云端模型名却静默跑本地最慢路径"的误导。
                const declared = String(parsed.declared_model || "");
                const ended = parsed.engine === "cloud" ? "cloud" : "local";   // 稳定枚举，渲染处再翻译
                const isLocalEngine = parsed.engine === "local";
                // sendMessage 里云配置已由 opts.cloudConfig 传递；模型名虽被选择但
                // 未匹配到云端配置 → 实际跑本地，正是要提醒用户的情形
                const cloudCfgPre = opts?.model
                  ? cloudModels.find((m) => m.name === opts.model)
                  : undefined;
                showPhase(t("agent.phase_analyze"));
                if (isLocalEngine && declared && !cloudCfgPre) {
                  // 本地模型选择是常态，不再弹窗打扰（09-04 用户反馈）——
                  // 路由信息保留在日志/routeInfo 中以便排查
                  console.info(`[route] 模型「${declared}」未在云端配置，实际运行在本地引擎`);
                }
                setRouteInfo({ engine: ended, declaredModel: declared });
                continue;
              }
              if (parsed.event === "route_fallback") {
                // 429 降级切换引擎（09-06：静默切换让用户以为还在用本地模型）
                const fbLocal = Boolean(parsed.is_local);
                const fbModel = String(parsed.declared_model || "");
                setRouteInfo({ engine: fbLocal ? "local" : "cloud", declaredModel: fbModel });
                showToast(String(parsed.message || t("app.model_switched")) + (fbModel ? ` (${fbModel})` : ""), "warn");
                continue;
              }
              if (parsed.event === "engine_wait") {
                // 多会话并行：本轮排在别的会话后面（后端闸门等待）。只在"正看这个
                // 会话"时改状态，避免 A 的等待提示画到 B 的界面上。
                showPhase(t("agent.phase_engine_wait"));
                continue;
              }
              if (parsed.event === "engine_recovering") {
                // 后端在等引擎（排队 / 自动重载）或模型静默期发的进度心跳。
                // 心跳本身就是数据 → 看门狗已随之续期；这里只把相位显示出来。
                // （2026-09-24 实测：此前后端的心跳被 _sample 丢弃，前端 180s
                // 收不到任何字节就误报"流式响应超时"，而后端仍在合法等待。）
                const ph = String(parsed.phase || "");
                const secs = Math.round(Number(parsed.waited) || 0);
                showPhase(ph === "reload"
                  ? t("agent.phase_engine_reload", { s: secs })
                  : ph === "queue"
                    ? t("agent.phase_engine_wait")
                    : t("agent.phase_engine_silent", { s: secs }));
                continue;
              }
              if (parsed.event === "engine_recovered") {
                // 数据回来了：把"等待引擎/静默"相位收回常规状态（否则会挂一整轮）
                showPhase(t("agent.phase_analyze"));
                continue;
              }
              if (parsed.event === "round_start") {
                // 轮次透明化（09-05 23:52：本地慢生成多轮拉锯时前端黑盒）
                currentRound = Number(parsed.iteration) || currentRound;
                flushStream();
                continue;
              }
              if (parsed.event === "tool_confirm") {
                flushStream();
                showPhase(t("agent.phase_confirm", { tool: parsed.tool || "" }));
                showToast(t("tool.confirm_toast", { tool: parsed.tool || "" }), "warn");
                // 等待用户确认可能超过看门狗时限——确认期间暂停看门狗，
                // 用户点允许/拒绝后 tool_start/tool_end 数据到达即自动恢复
                disarmWatchdog();
                writeMessages((prev) => {
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
                void autoAllow(parsed.call_id || "", parsed.tool, showToast, opts?.sessionId || session.id);
              } else if (parsed.event === "agent_plan") {
                flushStream();
                // 规划模式：执行计划显示为一条消息
                const plan = String(parsed.content ?? "");
                if (plan.trim()) {
                  writeMessages((prev) => [...prev, {
                    id: msgId(), role: "assistant",
                    content: `${t("app.plan_title")}\n\n${plan}`,
                  }]);
                }
              } else if (parsed.event === "plan_confirm") {
                // 计划确认门控：批准后才开始执行（后端在等这个 call_id 的决定）
                flushStream();
                disarmWatchdog();
                showToast(t("tool.confirm_toast", { tool: t("app.plan_tool") }), "warn");
                showPhase(t("agent.phase_confirm", { tool: t("app.plan_tool") }));
                writeMessages((prev) => [...prev, {
                  id: msgId(), role: "tool", type: "tool_call", content: "",
                  callId: parsed.call_id, toolName: t("app.plan_tool"),
                  toolArgs: parsed.args, toolStatus: "confirming",
                }]);
                void autoAllow(parsed.call_id || "", parsed.tool, showToast, opts?.sessionId || session.id);
              } else if (parsed.event === "reflection_revised") {
                flushStream();
                // 输出反思修正：把最后一条 assistant 消息替换为修正版。
                // 同步 full = revised，防止 [DONE] 时最终 flush 把修正前原文
                // 又以新气泡重复显示（M1 复盘 bug）。
                const revised = String(parsed.content ?? "");
                if (revised.trim()) {
                  full = revised;
                  streamFinalized = true;
                  thinkingAttached = true;
                  thinkingAttached = true; // 后续 flush 不再跑（否则 revised 被重复 push）
                  writeMessages((prev) => {
                    const msgs = [...prev];
                    for (let i = msgs.length - 1; i >= 0; i--) {
                      if (msgs[i].role === "assistant" && msgs[i].content && msgs[i].content.trim()) {
                        msgs[i] = { ...msgs[i], content: revised + "\n\n" + t("app.self_reviewed") };
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
                  // 注意：不置 thinkingAttached——定稿时最后一次 flush 仍需附着思考
                  writeMessages((prev) => {
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
                myStack().push(`${parsed.tool || ""} ${JSON.stringify(parsed.args || {}).slice(0, 60)}`);
                showStackTop();
                const startTs = Number(parsed.ts) || Date.now();
                writeMessages((prev) => {
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
                myStack().pop();
                showStackTop();
                const rawResult = String(parsed.result ?? "");
                const toolResult = rawResult.length > 10000
                  ? rawResult.slice(0, 10000) + "\n\n" + t("app.truncated")
                  : rawResult;
                const isError = rawResult.startsWith("Error") || rawResult.startsWith("⛔");
                const endTs = Number(parsed.ts) || Date.now();
                writeMessages((prev) => {
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
                // 思考内容：流式期间只攒不发（[DONE] 定稿时一次性附着，防闪现）
                if (!thinkingStartedAt) thinkingStartedAt = Date.now();
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

  const confirmTool = useCallback(async (callId: string, approved: boolean, always?: boolean, toolName?: string, sid?: string) => {
    // 双击/重复点击去重：同 callId 在途只发一次（09-21 实测：双击触发误报过期）
    if (confirmInFlightRef.current.has(callId)) return;
    confirmInFlightRef.current.add(callId);
    if (always && approved && toolName) {
      try {
        const key = alwaysAllowKey(sid || session.id);
        const arr: string[] = JSON.parse(localStorage.getItem(key) || "[]");
        if (!arr.includes(toolName)) {
          arr.push(toolName);
          localStorage.setItem(key, JSON.stringify(arr));
        }
        // 旧的全应用记忆清掉，避免再误自动通过
        try {
          const old = JSON.parse(localStorage.getItem("latiao_always_allow_tools") || "[]");
          if (old.includes(toolName)) {
            localStorage.setItem("latiao_always_allow_tools", JSON.stringify(old.filter((n: string) => n !== toolName)));
          }
        } catch { /* ignore */ }
      } catch { /* ignore */ }
    }
    try {
      const resp = await authFetch("/v1/confirm_tool", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ call_id: callId, approved }),
      });
      const data = await resp.json();
      if (data.status === "already") {
        // 服务端幂等：已处理（本卡已批准/拒绝）——收尾卡片状态，避免永远 confirming
        setMessagesFor(session.id, prev => prev.map(m => m.callId === callId && m.toolStatus === "confirming"
          ? { ...m, toolStatus: "done" as const } : m));
        return;
      }
      if (data.status === "not_found") {
        showToast(t("toast.timeout"));
        setMessagesFor(session.id, prev => prev.map(m => m.callId === callId && m.toolStatus === "confirming" ? { ...m, toolStatus: "error" as const, toolResult: t("toast.timeout_detail") } : m));
      }
    } catch (e) {
      console.error(e);
      // 服务端若已处理（21:01 实证：客户端失败但 approve 已生效）→ 重试一次再判定
      try {
        await new Promise(r => setTimeout(r, 300));
        const resp2 = await authFetch("/v1/confirm_tool", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ call_id: callId, approved }),
        });
        const d2 = await resp2.json();
        if (d2.status === "ok" || d2.status === "already") {
          setMessagesFor(session.id, prev => prev.map(m => m.callId === callId && m.toolStatus === "confirming"
            ? { ...m, toolStatus: "done" as const } : m));
          return;
        }
      } catch (e2) { console.error("confirm retry failed", e2); }
      showToast(t("toast.confirm_fail"));
    }
    finally { confirmInFlightRef.current.delete(callId); }
  }, [showToast, setMessagesFor, t, session.id]);

  // 停止**指定会话**（默认=当前查看的会话）的回合：控制器/取消请求/显示状态
  // 全部按会话定位（09-23）。此前用跟随视图的 sessionIdRef + 单一控制器，
  // 切走再点停止会停错会话。
  // ⚠️ 入参类型是 unknown 而不是 string：这个函数**同时**被当作按钮处理器用
  // （onStop={stopGeneration} → 子组件 onClick={onStop}），React 会把点击事件对象
  // 塞进来。事件对象带 DOM 引用（循环结构）→ 之后 JSON.stringify 直接抛
  // TypeError，真机 2026-09-23 崩过一次。所以这里只认非空字符串，其余回落到
  // sessionIdRef。类型写成 unknown 是刻意的：让将来再想直接挂它也躲不过检查。
  const stopGeneration = useCallback((targetSession?: unknown) => {
    const sid = resolveStopTarget(targetSession, sessionIdRef.current);
    const ctl = abortControllersRef.current[sid];
    if (ctl) ctl.abort();
    abortControllersRef.current[sid] = null;
    // 服务端取消：仅断前端流时 agent 循环会继续烧 GPU/执行工具/扣云端
    // 费用（P0）。置位 sidecar 会话级取消标记，循环在每轮迭代/工具执行前
    // 检查并中止。
    authFetch("/v1/chat/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sid }),
    }).catch(() => { /* sidecar 不可达时仅靠 abort 兜底 */ });
    taskStacksRef.current[sid] = [];
    delete taskStartsRef.current[sid];
    delete processingRef.current[sid];          // 同步镜像（state 更新滞后一拍）
    setProcessing((p) => { const n = { ...p }; delete n[sid]; return n; });
    if (sessionIdRef.current === sid) {
      setActiveTask(null);
      setTaskStartAt(null);
      setAgentPhase("");
    }
    // refs 与 setState 均为稳定引用，写进 deps 只为满足 exhaustive-deps
  }, [processingRef, sessionIdRef, setActiveTask, setAgentPhase, setProcessing, setTaskStartAt, abortControllersRef, taskStacksRef, taskStartsRef]);

  /* ── Send Message ── */

  return { streamChat, confirmTool, stopGeneration, taskStacksRef, taskStartsRef, abortControllersRef };
}
