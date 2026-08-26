import { memo, lazy, Suspense, useCallback, useState, useMemo, useRef, useEffect } from "react";
import type { Message, PendingFile } from "../types";
import { useTranslation } from "../i18n";
import ToolCallBubble from "./ToolCallBubble";
import ToolbarSelect from "./ToolbarSelect";
import { Eye, ShieldCheck, PencilRuler, ListChecks, Zap, CircleOff, Brain, BrainCircuit, Bot, User, ChevronRight, ChevronDown } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { openUrl } from "@tauri-apps/plugin-opener";
import remarkGfm from "remark-gfm";

const SyntaxHighlighter = lazy(async () => {
  const [{ Prism }, { oneDark }] = await Promise.all([
    import("react-syntax-highlighter"),
    import("react-syntax-highlighter/dist/esm/styles/prism"),
  ]);
  // ZCode 式代码块：去掉 oneDark 的深色面板背景/圆角/内边距，
  // 语法配色保留，融入卡片背景（无框）
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const flatTheme: Record<string, any> = Object.fromEntries(
    Object.entries(oneDark).map(([k, v]) => [k, { ...(v as object), background: "transparent" }])
  );
  flatTheme['pre[class*="language-"]'] = {
    ...flatTheme['pre[class*="language-"]'],
    background: "transparent", margin: 0, padding: 0, boxShadow: "none",
  };
  flatTheme['code[class*="language-"]'] = {
    ...flatTheme['code[class*="language-"]'],
    background: "transparent", boxShadow: "none", textShadow: "none",
  };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return { default: (props: any) => <Prism style={flatTheme} {...props} /> };
});

function CodeBlock({ language, children }: { language: string; children: string }) {
  return (
    <Suspense fallback={<pre><code>{children}</code></pre>}>
      <SyntaxHighlighter language={language} PreTag="div">
        {children}
      </SyntaxHighlighter>
    </Suspense>
  );
}

interface ChatViewProps {
  messages: Message[];
  isProcessing: boolean;
  pendingFile: PendingFile | null;
  setPendingFile: (f: PendingFile | null) => void;
  prompt: string;
  setPrompt: (p: string) => void;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  mediaRecorderRef: React.MutableRefObject<MediaRecorder | null>;
  isRecording: boolean;
  onStop: () => void;
  sendMessage: () => void;
  handleFileSelect: (e: React.ChangeEvent<HTMLInputElement>) => void;
  startRecording: () => void;
  confirmTool: (callId: string, approved: boolean) => void;
  chatEndRef: React.RefObject<HTMLDivElement | null>;
  handleDrop?: (e: React.DragEvent) => void;
  onPasteImage?: (file: File) => void;
  cloudModels: { name: string }[];
  selectedModel: string;
  onSelectModel: (m: string) => void;
  accessMode: "read_only" | "confirm" | "auto_edit" | "plan" | "full";
  setAccessMode: (m: "read_only" | "confirm" | "auto_edit" | "plan" | "full") => void;
  thinkingLevel: "off" | "high" | "max";
  setThinkingLevel: (l: "off" | "high" | "max") => void;
  contextEstimate?: { max_context: number; recommended_context: number } | null;
  showToast: (msg: string, type?: string) => void;
  activeTask: string | null;
  taskStartAt: number | null;
}

export default memo(function ChatView({
  messages, isProcessing, pendingFile, setPendingFile,
  prompt, setPrompt,
  fileInputRef, mediaRecorderRef, isRecording,
  sendMessage, onStop, handleFileSelect, startRecording, confirmTool,
  chatEndRef, handleDrop, onPasteImage,
  cloudModels, selectedModel, onSelectModel,
  accessMode, setAccessMode, thinkingLevel, setThinkingLevel,
  contextEstimate, showToast, activeTask, taskStartAt,
}: ChatViewProps) {
  const { t } = useTranslation();
  const handleEditableKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey && !isProcessing) {
      e.preventDefault();
      sendMessage();
    }
  }, [sendMessage, isProcessing]);

  const handleSend = useCallback(() => {
    sendMessage();
  }, [sendMessage]);

  // 点赞/点踩本地反馈状态（localStorage 持久化，可扩展为反馈学习数据）
  const [msgFeedback, setMsgFeedback] = useState<Record<string, "up" | "down">>(() => {
    try { return JSON.parse(localStorage.getItem("latiao_msg_feedback") || "{}"); } catch { return {}; }
  });
  const toggleFeedback = (msgId: string, kind: "up" | "down") => {
    const cur = msgFeedback[msgId];
    const next = cur === kind ? undefined : kind;
    const m = { ...msgFeedback };
    if (next) m[msgId] = next; else delete m[msgId];
    setMsgFeedback(m);
    try { localStorage.setItem("latiao_msg_feedback", JSON.stringify(m)); } catch { /* ignore */ }
  };
  const fmtTime = (ts?: number) => ts ? new Date(ts).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }) : "";

  // ── 消息导航缩略图（minimap）：长会话右侧迷你图，点击/拖动跳转 ──
  const scrollRef = useRef<HTMLDivElement>(null);
  const [scrollInfo, setScrollInfo] = useState({ top: 0, viewH: 0, totalH: 1 });
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => setScrollInfo({ top: el.scrollTop, viewH: el.clientHeight, totalH: el.scrollHeight || 1 });
    onScroll();
    el.addEventListener("scroll", onScroll, { passive: true });
    const ro = new ResizeObserver(onScroll);
    ro.observe(el);
    return () => { el.removeEventListener("scroll", onScroll); ro.disconnect(); };
  }, [messages.length]);

  // 每条消息 → 色块（均匀等高，ZCode 风格；仅颜色区分角色）
  const miniBlocks = useMemo(() => messages.map((m, i) => ({
    index: i,
    color: m.role === "user" ? "var(--accent)" : m.role === "tool" ? "var(--text-disabled)" : "var(--success)",
    key: m.id || `m${i}`,
  })), [messages]);

  const jumpTo = (ratio: number) => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = ratio * (el.scrollHeight - el.clientHeight);
  };

  // 每次工具调用独立成行（活动摘要行 ZCode 式），不再分组折叠
  // minimap 悬停预览：hoverRatio + 对应消息预览
  const [minimapHover, setMinimapHover] = useState<{ ratio: number; idx: number } | null>(null);

  // 任务头部"已工作 X 分 X 秒"计时（isProcessing / 工具执行期间显示）
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!taskStartAt || (!isProcessing && !activeTask)) { setElapsed(0); return; }
    const tick = () => setElapsed(Date.now() - taskStartAt);
    tick();
    const iv = setInterval(tick, 1000);
    return () => clearInterval(iv);
  }, [taskStartAt, isProcessing, activeTask]);

  // 时长格式化：<3s 显示"几秒"，长于 60s 显示"X 分 X 秒"
  const fmtDur = (ms: number) => {
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return `${s} 秒`;
    return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  };

  // ── 对话分段（ZCode 式：一次对话 = 一张卡片，头部显示耗时）──
  // 每轮顺序固定为 [user, tool…, assistant]（App.tsx 已保证插入位置）；
  // 兼容历史坏数据：段首工具挪到问题之后；无 user 的孤儿工具段并入下一段
  const segments = useMemo(() => {
    const raw: { msgs: Message[] }[] = [];
    let cur: Message[] = [];
    const push = () => {
      if (cur.length === 0) return;
      raw.push({ msgs: cur });
      cur = [];
    };
    for (const m of messages) {
      if (m.role === "user" && cur.length > 0) { push(); cur = [m]; }
      else cur.push(m);
    }
    push();
    // 孤儿段（历史坏顺序的纯工具段）并入相邻段：前面有段并入其末尾，否则挂起并入下一段
    const merged: { msgs: Message[] }[] = [];
    let pending: Message[] = [];
    for (const seg of raw) {
      const hasUser = seg.msgs.some((m) => m.role === "user");
      if (!hasUser) {
        if (merged.length > 0) {
          const last = merged[merged.length - 1];
          last.msgs = [...last.msgs, ...seg.msgs];
        } else {
          pending = [...pending, ...seg.msgs];
        }
      } else {
        if (pending.length > 0) { seg.msgs = [...pending, ...seg.msgs]; pending = []; }
        merged.push(seg);
      }
    }
    if (pending.length > 0) {
      if (merged.length > 0) {
        const last = merged[merged.length - 1];
        last.msgs = [...last.msgs, ...pending];
      } else {
        merged.push({ msgs: pending });
      }
    }
    // normalize：段首连续工具消息挪到该段 user 消息之后
    const segs: { startTs?: number; endTs?: number; msgs: Message[] }[] = [];
    for (const seg of merged) {
      let msgs = seg.msgs;
      if (msgs[0]?.role === "tool" || msgs[0]?.type === "tool_call") {
        let k = 0;
        while (k < msgs.length && (msgs[k].role === "tool" || msgs[k].type === "tool_call")) k++;
        if (k < msgs.length) msgs = [msgs[k], ...msgs.slice(0, k), ...msgs.slice(k + 1)];
      }
      let st = msgs[0].ts, en = 0;
      for (const m of msgs) {
        const t = (m.ts || 0) + (m.duration || 0) + (m.thinkingDuration || 0);
        if (t > en) en = t;
        if (m.ts && (!st || m.ts < st)) st = m.ts;
      }
      segs.push({ startTs: st, endTs: en, msgs });
    }
    return segs;
  }, [messages]);
  const [collapsedSegs, setCollapsedSegs] = useState<Record<string, boolean>>({});

  // 单条消息渲染（段内复用；hideActivities=头部折叠时隐藏思考/工具，正文常驻）
  const renderMsg = (msg: Message, i: number, hideActivities: boolean) => {
          if (msg.role === "tool" || msg.type === "tool_call") {
            if (hideActivities) return null;
            // 活动摘要行（ZCode 式）：每个工具调用独立一行，点击展开结果
            return <ToolCallBubble key={msg.id || i} msg={msg} onConfirm={confirmTool} />;
          }
          if (msg.role === "assistant") {
            return (
              <div key={msg.id || i} className={`msg-row assistant${msg.type === "file" ? " file" : ""}`}>
                <div className="avatar-small avatar-bot"><Bot size={19} strokeWidth={2} /></div>
                <div className="msg-content">
                  <div className="msg-bubble assistant">
                    {(() => {
                      // 思考内容显示：后端 reasoning 字段（推理模型）或本地 <think> 标签
                      const localThink = msg.content.match(/^<think>([\s\S]*?)<\/think>\s*/);
                      const thinkText = msg.thinking || (localThink ? localThink[1] : null);
                      const bodyText = localThink ? msg.content.slice(localThink[0].length) : msg.content;
                      return (
                        <>
                          {thinkText && !hideActivities && (
                            <details className="thinking-block">
                              <summary className="thinking-summary">
                                <Brain size={13} />
                                <span>思考过程</span>
                                {msg.thinkingDuration !== undefined && (
                                  <span className="thinking-meta">· 持续了 {fmtDur(msg.thinkingDuration)}</span>
                                )}
                              </summary>
                              <div>{thinkText}</div>
                            </details>
                          )}
                          {bodyText && (
                            <ReactMarkdown remarkPlugins={[remarkGfm]} components={
                              {
                                code: ({ inline, className, children, ...props }: { inline?: boolean; className?: string; children?: React.ReactNode }) =>
                                  inline
                                    ? <code className={className} {...props}>{children}</code>
                                    : <CodeBlock language={(className || "").replace("language-", "")}>{String(children)}</CodeBlock>,
                                a: ({ href, children }: { href?: string; children?: React.ReactNode }) => (
                                  <a href={href} onClick={(e) => { e.preventDefault(); if (href) openUrl(href); }}>{children}</a>
                                ),
                              }
                            }>{bodyText}</ReactMarkdown>
                          )}
                        </>
                      );
                    })()}
                  </div>
                  <div className="msg-actions">
                    <button className="btn-icon" title={t("chat.copy")} onClick={() => {
                      navigator.clipboard?.writeText(msg.content).then(() => showToast(t("chat.copied"))).catch(() => showToast(t("chat.copy_fail"), "warn"));
                    }}>⧉</button>
                    <button className={`btn-icon${msgFeedback[msg.id || ""] === "up" ? " active" : ""}`} title={t("chat.like")}
                      onClick={() => msg.id && toggleFeedback(msg.id, "up")}>👍</button>
                    <button className={`btn-icon${msgFeedback[msg.id || ""] === "down" ? " active" : ""}`} title={t("chat.dislike")}
                      onClick={() => msg.id && toggleFeedback(msg.id, "down")}>👎</button>
                    {fmtTime(msg.ts) && <span className="msg-time">{fmtTime(msg.ts)}</span>}
                  </div>
                </div>
              </div>
            );
          }
          if (msg.type === "file") {
            return (
              <div key={msg.id || i} className={`msg-row user file`}>
                <div className="avatar-small avatar-user"><User size={19} strokeWidth={2} /></div>
                <div className="msg-content">
                  <div className="msg-bubble">
                    {msg.imagePreview ? (
                      <img src={msg.imagePreview} alt="" style={{ maxWidth: 240 }} />
                    ) : (
                      <span>📄 {msg.filename}</span>
                    )}
                  </div>
                  {msg.content && <div className="msg-bubble" style={{ marginTop: 6 }}>{msg.content}</div>}
                </div>
              </div>
            );
          }
          return (
            <div key={msg.id || i} className={`msg-row user${msg.type === "image" ? " file" : ""}`}>
              <div className="avatar-small avatar-user"><User size={19} strokeWidth={2} /></div>
              <div className="msg-content">
                <div className="msg-bubble user">{msg.content}</div>
                <div className="msg-actions">
                  <button className="btn-icon" title={t("chat.copy")} onClick={() => {
                    navigator.clipboard?.writeText(msg.content).then(() => showToast(t("chat.copied"))).catch(() => showToast(t("chat.copy_fail"), "warn"));
                  }}>⧉</button>
                  {fmtTime(msg.ts) && <span className="msg-time">{fmtTime(msg.ts)}</span>}
                </div>
              </div>
            </div>
          );
  };

  // 状态栏数据：轮数（user 消息数）、工具调用数、消息数、token 估算
  const userTurns = messages.filter(m => m.role === "user").length || 0;
  const toolCalls = messages.filter(m => m.role === "tool" || m.type === "tool_call").length || 0;
  const estTokens = Math.round(messages.reduce((s, m) => s + m.content.length, 0) * 0.55); // 中文近似

  return (
    <>
      <div className="chat-wrap">
      {/* 任务头部：已工作计时（工具执行 / 回复生成期间显示） */}
      {taskStartAt && (activeTask || isProcessing) && (
        <div className="task-statusbar">
          <span className="task-statusbar-dot">▣</span>
          <span className="task-statusbar-label">已工作 {fmtDur(elapsed)}</span>
          {activeTask && <span className="task-statusbar-sub">{activeTask.slice(0, 60)}</span>}
        </div>
      )}
      {messages.length > 8 && (
        <>
        <div className="chat-minimap" onClick={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          jumpTo((e.clientY - rect.top) / rect.height);
        }} onMouseMove={(e) => {
          const rect = e.currentTarget.getBoundingClientRect();
          const ratio = Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height));
          // 均匀块：索引 = ratio * 消息数
          const idx = Math.min(miniBlocks.length - 1, Math.floor(ratio * miniBlocks.length));
          setMinimapHover({ ratio, idx });
        }} onMouseLeave={() => setMinimapHover(null)}>
          {miniBlocks.map(b => (
            <div key={b.key} className={`mini-block${minimapHover && minimapHover.idx === b.index ? " hover" : ""}`}
              style={{ background: b.color }} />
          ))}
          <div className="mini-viewport" style={{
            top: `${(scrollInfo.top / Math.max(1, scrollInfo.totalH - scrollInfo.viewH)) * 100}%`,
            height: `${(scrollInfo.viewH / scrollInfo.totalH) * 100}%`,
          }} />
        </div>
        {/* 悬停预览浮层（ZCode 式：以悬停消息为中心的滑动窗口对话流） */}
        {minimapHover && messages[minimapHover.idx] && (() => {
          // 以悬停消息为中心：上下各 4 条；工具逐条展开（不折叠），保证滑过每张卡片内容都不同
          const cur = minimapHover.idx;
          const lo = Math.max(0, cur - 4);
          const hi = Math.min(messages.length - 1, cur + 4);
          const entries: { icon: string; text: string; isCurrent: boolean; key: string }[] = [];
          for (let i = lo; i <= hi; i++) {
            const m = messages[i];
            const isCur = i === cur;
            if (m.role === "tool") {
              let args = "";
              try {
                args = m.toolArgs
                  ? JSON.stringify(m.toolArgs).replace(/[{}"]/g, "").replace(/[:,]/g, " ").replace(/\s+/g, " ").trim()
                  : "";
              } catch { /* 参数不可序列化时忽略 */ }
              entries.push({
                icon: "◆",
                text: `${m.toolName || "工具"}${args ? " · " + args.slice(0, 48) : ""}`.slice(0, 80),
                isCurrent: isCur, key: m.id || `t${i}`,
              });
            } else if ((m.content || "").trim()) {
              const text = (m.content || "").replace(/[#*|`>-]/g, "").replace(/\s+/g, " ").slice(0, 110);
              entries.push({
                icon: m.role === "user" ? "🧑" : "🤖",
                text, isCurrent: isCur, key: m.id || `m${i}`,
              });
            }
          }
          if (entries.length === 0) return null;
          // 预览跟随悬停位置上下滑动（clamp 到可视范围内）
          const follow = Math.max(0, Math.min(scrollInfo.viewH - 320, minimapHover.ratio * Math.max(0, scrollInfo.viewH - 320)));
          return (
            <div className="mini-preview" style={{ top: 34 + follow }}>
              {entries.map(e => (
                <div key={e.key} className={`mini-preview-line${e.isCurrent ? " current" : ""}`}>
                  <span className="mini-preview-icon">{e.icon}</span>
                  <span>{e.text}</span>
                </div>
              ))}
            </div>
          );
        })()}
        </>
      )}
      <div className="chat-scroll" ref={scrollRef} onDrop={handleDrop} onDragOver={(e) => { if (handleDrop) e.preventDefault(); }}>
        {segments.map((seg, si) => {
          const segKey = seg.msgs[0]?.id || `seg${si}`;
          const collapsed = collapsedSegs[segKey] === true;
          const segDur = seg.startTs && seg.endTs ? Math.max(0, seg.endTs - seg.startTs) : 0;
          const qMsg = seg.msgs.find((m) => m.role === "user");
          const qText = (qMsg?.content || "").replace(/\s+/g, " ").slice(0, 24);
          // 进行中的对话段（最后一段）：实时计时
          const live = si === segments.length - 1 && taskStartAt !== null && (isProcessing || activeTask !== null);
          const label = live
            ? `${qText ? qText + " · " : ""}已工作 ${fmtDur(elapsed)}`
            : segDur > 0
              ? `${qText ? qText + " · " : ""}已工作 ${fmtDur(segDur)}`
              : qText || "对话";
          return (
            <div key={segKey} className={`chat-segment${collapsed ? " collapsed" : ""}`}>
              <button className="chat-segment-head" onClick={() => setCollapsedSegs(p => ({ ...p, [segKey]: !collapsed }))}>
                <span className="chat-segment-dot">▣</span>
                <span className="chat-segment-label">{label}</span>
                <span className="chat-segment-chevron">{collapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}</span>
              </button>
              {seg.msgs.map((m, i) => renderMsg(m, i, collapsed))}
            </div>
          );
        })}
        <div ref={chatEndRef}></div>
      </div>
      </div>

      <div className="input-area">
        {pendingFile && (
          <div className="file-preview">
            <div className={`file-preview-thumb ${pendingFile.type === "pdf" ? "pdf" : "code"}`}>
              {pendingFile.type === "image" ? <img src={pendingFile.preview} alt="" style={{ width: 38, height: 38, borderRadius: 4, objectFit: "cover" }} /> : pendingFile.preview === "📄" ? "📄" : "📄"}
            </div>
            <span className="file-preview-name">{pendingFile.name}</span>
            <button className="file-preview-close" onClick={() => setPendingFile(null)}>✕</button>
          </div>
        )}
        <div className="input-row">
          <textarea
            className={`chat-input${prompt ? "" : " is-empty"}`}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={handleEditableKeyDown}
            onPaste={async (e) => {
              if (!onPasteImage) return;
              const items = e.clipboardData?.items;
              if (!items) return;
              for (const item of Array.from(items)) {
                if (item.type.startsWith("image/")) {
                  e.preventDefault();
                  const file = item.getAsFile();
                  if (file) onPasteImage(file);
                  return;
                }
              }
            }}
            placeholder={t("chat.placeholder")}
            disabled={isProcessing}
            rows={1}
            style={{ resize: "none", minHeight: 52, maxHeight: 150 }}
          />
          <input type="file" ref={fileInputRef} style={{ display: "none" }} onChange={handleFileSelect} />
          {/* 底部工具栏：左（附件/语音/模式）右（模型/设置/发送） */}
          <div className="input-toolbar">
            <div className="toolbar-left">
              <button className="btn-icon" onClick={() => fileInputRef.current?.click()} title={t("chat.attach")}>＋</button>
              <button className="btn-icon" onClick={isRecording ? () => mediaRecorderRef.current?.stop() : startRecording}
                style={isRecording ? { color: "var(--danger)" } : undefined} title={t("chat.voice")}>{isRecording ? "⏹" : "🎙"}</button>
              <ToolbarSelect value={accessMode}
                options={[
                  { value: "read_only", label: t("chat.access_readonly"), icon: <Eye size={15} /> },
                  { value: "confirm", label: t("chat.access_confirm"), icon: <ShieldCheck size={15} /> },
                  { value: "auto_edit", label: t("chat.access_auto_edit"), icon: <PencilRuler size={15} /> },
                  { value: "plan", label: t("chat.access_plan"), icon: <ListChecks size={15} /> },
                  { value: "full", label: t("chat.access_full"), icon: <Zap size={15} /> },
                ]}
                onChange={(v) => setAccessMode(v as "read_only" | "confirm" | "auto_edit" | "plan" | "full")}
                title={t("chat.access_title")} />
              <ToolbarSelect value={thinkingLevel}
                options={[
                  { value: "off", label: t("chat.thinking_off"), icon: <CircleOff size={15} /> },
                  { value: "high", label: t("chat.thinking_high"), icon: <Brain size={15} /> },
                  { value: "max", label: t("chat.thinking_max"), icon: <BrainCircuit size={15} /> },
                ]}
                onChange={(v) => setThinkingLevel(v as "off" | "high" | "max")}
                title={t("chat.thinking_title")} />
            </div>
            <div className="toolbar-right">
              <select className="form-input" style={{
                fontSize: 13, padding: "2px 6px", margin: 0, width: "auto", maxWidth: 180,
                background: "transparent", border: "0", color: "var(--text-secondary)",
                cursor: "pointer", outline: "none",
              }}
                value={selectedModel} onChange={(e) => onSelectModel(e.target.value)}
                title={t("chat.model_select")}>
                <option value="">{t("sidebar.auto_detect")}</option>
                {cloudModels.map((m) => (
                  <option key={m.name} value={m.name}>☁️ {m.name}</option>
                ))}
              </select>
              {isProcessing ? (
                <button className="btn-send btn-circle" onClick={onStop} title={t("chat.stop")}>⏹</button>
              ) : (
                <button className="btn-send btn-circle" onClick={handleSend} title={t("chat.send")}>↑</button>
              )}
            </div>
          </div>
        </div>
        {/* 会话状态栏 */}
        <div className="chat-statusbar">
          {userTurns > 0 ? (
            <>
              <span>{userTurns} 轮 · {toolCalls} 次工具调用</span>
              <span className="statusbar-sep">|</span>
              <span>{messages.length} 条消息</span>
              <span className="statusbar-sep">|</span>
              <span>~{estTokens.toLocaleString()} tokens</span>
              {contextEstimate?.max_context ? (
                <>
                  <span className="statusbar-sep">|</span>
                  <span>上下文 {Math.round((estTokens / contextEstimate.max_context) * 100)}%</span>
                </>
              ) : null}
            </>
          ) : (
            <span>{t("chat.status_hint")}</span>
          )}
        </div>
      </div>
    </>
  );
});
