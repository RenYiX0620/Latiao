import { memo, lazy, Suspense, useRef, useCallback, useEffect } from "react";
import type { Message, PendingFile } from "../types";
import { useTranslation } from "../i18n";
import ToolCallBubble from "./ToolCallBubble";
import ReactMarkdown from "react-markdown";
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
}

export default memo(function ChatView({
  messages, isProcessing, pendingFile, setPendingFile,
  prompt, setPrompt, planMode, setPlanMode,
  fileInputRef, mediaRecorderRef, isRecording,
  sendMessage, onStop, handleFileSelect, startRecording, confirmTool,
  chatEndRef, handleDrop, onPasteImage,
}: ChatViewProps) {
  const { t } = useTranslation();
  const editableRef = useRef<HTMLDivElement>(null);
  const promptRef = useRef(prompt);
  useEffect(() => { promptRef.current = prompt; }, [prompt]);

  const handleEditableInput = useCallback(() => {
    const el = editableRef.current;
    if (!el) return;
    setPrompt(el.textContent || "");
  }, [setPrompt]);

  const handleSend = useCallback(() => {
    sendMessage();
    // Clear the editable div after send (state handled by sendMessage's setPrompt(""))
    setTimeout(() => {
      if (editableRef.current && promptRef.current === "") {
        editableRef.current.textContent = "";
      }
    }, 0);
  }, [sendMessage]);

  const handleEditableKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }, [handleSend]);

  return (
    <>
      <div className="chat-area"
        onDrop={handleDrop}
        onDragOver={e => e.preventDefault()}>
        {messages.length === 0 ? (
          <div className="chat-empty">
            <div className="chat-empty-icon">💬</div>
            <div className="chat-empty-title">{t("chat.empty_title")}</div>
            <div className="chat-empty-desc" style={{ fontSize: 14, marginTop: -4 }}>Latiao</div>
            <div className="chat-empty-desc" style={{ marginTop: 12 }}>{t("chat.empty_desc")}</div>
          </div>
        ) : (
          messages.map((msg, i) => {
            if (msg.type === "tool_call" || msg.role === "tool") {
              return (
                <div key={i} className="msg">
                  <div className="msg-role">{t("chat.role_assistant")}</div>
                  <ToolCallBubble msg={msg} onConfirm={confirmTool} />
                </div>
              );
            }
            return (
              <div key={i} className={`msg${msg.role === "user" ? " user" : ""}`}>
                <div className="msg-role">{msg.role === "user" ? t("chat.role_you") : t("chat.role_assistant")}</div>
                {msg.role === "user" && msg.imagePreview ? (
                  <div>
                    {msg.imagePreview && <img src={msg.imagePreview} alt={msg.filename || "image"} style={{ maxWidth: 260, borderRadius: 8, marginBottom: 8 }} />}
                    {msg.content && <div className="msg-bubble user">{msg.content}</div>}
                  </div>
                ) : (
                  <div className={`msg-bubble ${msg.role}`}>
                    {msg.role === "assistant" ? (
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        components={{
                          code({ className, children, ...props }) {
                            const match = /language-(\w+)/.exec(className || "");
                            const codeStr = String(children).replace(/\n$/, "");
                            const nodeProps = props as Record<string, unknown>;
                            const inline = nodeProps.inline;
                            return !inline && match ? (
                              <CodeBlock language={match[1]}>{codeStr}</CodeBlock>
                            ) : (<code className={className} {...props}>{children}</code>);
                          },
                        }}
                      >
                        {(msg.content || "").replace(/```tool\s+\w+\s*\n\{[^}]*\}\n```/g, "").replace(/^```\s*\n?/gm, "").trim()}
                      </ReactMarkdown>
                    ) : (msg.content)}
                  </div>
                )}
              </div>
            );
          })
        )}
        {isProcessing && (
          <div className="processing">
            <span style={{ fontSize: 11 }}>{t("chat.processing")}</span>
            <span className="processing-dot"></span>
            <span className="processing-dot"></span>
            <span className="processing-dot"></span>
          </div>
        )}
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
          <div
            ref={editableRef}
            className={`chat-input${prompt ? "" : " is-empty"}`}
            contentEditable={!isProcessing}
            onInput={handleEditableInput}
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
            data-placeholder={t("chat.placeholder")}
            role="textbox"
            aria-multiline="true"
          />
          <input type="file" ref={fileInputRef} style={{ display: "none" }} onChange={handleFileSelect} />
          <button className="btn-icon" onClick={() => fileInputRef.current?.click()} title={t("chat.attach")}>📎</button>
          <button className="btn-icon" onClick={isRecording ? () => mediaRecorderRef.current?.stop() : startRecording}
            style={isRecording ? { color: "var(--danger)" } : undefined} title={t("chat.voice")}>{isRecording ? "⏹" : "🎙"}</button>
          <button className="btn btn-sm btn-ghost"
            style={{ padding: "4px 10px", fontSize: 10, marginRight: 4, background: planMode ? "var(--accent-soft)" : "transparent", borderColor: planMode ? "var(--border-accent)" : undefined, color: planMode ? "var(--accent)" : undefined }}
            onClick={() => setPlanMode(!planMode)} title={t("chat.plan_mode")}>
            📋 {t("chat.plan_mode_btn")}
          </button>
          {isProcessing ? (
            <button className="btn-stop" onClick={onStop}>⏹ {t("chat.stop")}</button>
          ) : (
            <button className="btn-send" onClick={handleSend}>{t("chat.send")}</button>
          )}
        </div>
      </div>
    </>
  );
});
