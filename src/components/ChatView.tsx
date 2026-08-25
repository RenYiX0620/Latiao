import { memo, lazy, Suspense, useCallback, useState } from "react";
import type { Message, PendingFile } from "../types";
import { useTranslation } from "../i18n";
import ToolCallBubble from "./ToolCallBubble";
import ReactMarkdown from "react-markdown";
import { openUrl } from "@tauri-apps/plugin-opener";
import remarkGfm from "remark-gfm";

const SyntaxHighlighter = lazy(async () => {
  const [{ Prism }, { oneDark }] = await Promise.all([
    import("react-syntax-highlighter"),
    import("react-syntax-highlighter/dist/esm/styles/prism"),
  ]);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return { default: (props: any) => <Prism style={oneDark} {...props} /> };
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
  planMode: boolean;
  setPlanMode: (v: boolean) => void;
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
  contextEstimate?: { max_context: number; recommended_context: number } | null;
  showToast: (msg: string, type?: string) => void;
}

export default memo(function ChatView({
  messages, isProcessing, pendingFile, setPendingFile,
  prompt, setPrompt, planMode, setPlanMode,
  fileInputRef, mediaRecorderRef, isRecording,
  sendMessage, onStop, handleFileSelect, startRecording, confirmTool,
  chatEndRef, handleDrop, onPasteImage,
  cloudModels, selectedModel, onSelectModel,
  accessMode, setAccessMode, contextEstimate, showToast,
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

  // 状态栏数据：轮数（user 消息数）、工具调用数、消息数、token 估算
  const userTurns = messages.filter(m => m.role === "user").length || 0;
  const toolCalls = messages.filter(m => m.role === "tool" || m.type === "tool_call").length || 0;
  const estTokens = Math.round(messages.reduce((s, m) => s + m.content.length, 0) * 0.55); // 中文近似

  return (
    <>
      <div className="chat-scroll" onDrop={handleDrop} onDragOver={(e) => { if (handleDrop) e.preventDefault(); }}>
        {messages.map((msg, i) => {
          if (msg.role === "tool" || msg.type === "tool_call") {
            return <ToolCallBubble key={msg.id || i} msg={msg} onConfirm={confirmTool} />;
          }
          if (msg.role === "assistant") {
            return (
              <div key={msg.id || i} className={`msg-row assistant${msg.type === "file" ? " file" : ""}`}>
                <div className="avatar-small">🤖</div>
                <div className="msg-content">
                  <div className="msg-bubble">
                    <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
                      code: ({ inline, className, children, ...props }: { inline?: boolean; className?: string; children?: React.ReactNode }) =>
                        inline
                          ? <code className={className} {...props}>{children}</code>
                          : <CodeBlock language={(className || "").replace("language-", "")}>{String(children)}</CodeBlock>,
                      a: ({ href, children }: { href?: string; children?: React.ReactNode }) => (
                        <a href={href} onClick={(e) => { e.preventDefault(); if (href) openUrl(href); }}>{children}</a>
                      ),
                    }}>
                      {msg.content}
                    </ReactMarkdown>
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
                <div className="avatar-small">🧑</div>
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
              <div className="avatar-small">🧑</div>
              <div className="msg-content">
                <div className="msg-bubble">{msg.content}</div>
                <div className="msg-actions">
                  <button className="btn-icon" title={t("chat.copy")} onClick={() => {
                    navigator.clipboard?.writeText(msg.content).then(() => showToast(t("chat.copied"))).catch(() => showToast(t("chat.copy_fail"), "warn"));
                  }}>⧉</button>
                  {fmtTime(msg.ts) && <span className="msg-time">{fmtTime(msg.ts)}</span>}
                </div>
              </div>
            </div>
          );
        })}
        <div ref={chatEndRef}></div>
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
              <button className="btn btn-sm btn-ghost"
                style={{ padding: "3px 10px", fontSize: 10, background: planMode ? "var(--accent-soft)" : "transparent", borderColor: planMode ? "var(--border-accent)" : undefined, color: planMode ? "var(--accent)" : undefined }}
                onClick={() => setPlanMode(!planMode)} title={t("chat.plan_mode")}>
                📋 {planMode ? t("chat.plan_mode_on") : t("chat.plan_mode_btn")}
              </button>
              <select className="form-input" style={{
                fontSize: 10, padding: "2px 6px", margin: 0, width: "auto", maxWidth: 130,
                background: "transparent", border: "0", color: "var(--text-secondary)",
                cursor: "pointer", outline: "none",
              }} value={accessMode} onChange={(e) => setAccessMode(e.target.value as "read_only" | "confirm" | "auto_edit" | "plan" | "full")}
                title={t("chat.access_title")}>
                <option value="read_only">🛡 {t("chat.access_readonly")}</option>
                <option value="confirm">✋ {t("chat.access_confirm")}</option>
                <option value="auto_edit">✅ {t("chat.access_auto_edit")}</option>
                <option value="plan">📋 {t("chat.access_plan")}</option>
                <option value="full">⚡ {t("chat.access_full")}</option>
              </select>
            </div>
            <div className="toolbar-right">
              <select className="form-input" style={{
                fontSize: 11, padding: "2px 6px", margin: 0, width: "auto", maxWidth: 180,
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
