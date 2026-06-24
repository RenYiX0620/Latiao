import { useState, useMemo, memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useTranslation } from "../i18n";
import { openUrl } from "@tauri-apps/plugin-opener";
import type { Message } from "../types";

const MAX_PREVIEW_CHARS = 2000;

function formatToolArgs(args?: Record<string, unknown>): string {
  if (!args) return "";
  const entries = Object.entries(args);
  if (entries.length === 0) return "";
  const [key, value] = entries[0];
  const valStr = typeof value === "string" ? value : JSON.stringify(value);
  return `${key}: ${valStr.length > 50 ? valStr.slice(0, 50) + "..." : valStr}`;
}

const ToolCallBubble = memo(function ToolCallBubble({ msg, onConfirm }: {
  msg: Message;
  onConfirm?: (callId: string, approved: boolean) => void;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const [fullExpanded, setFullExpanded] = useState(false);
  const statusClass = msg.toolStatus === "confirming" ? "confirming" : msg.toolStatus === "running" ? "running" : msg.toolStatus === "error" ? "error" : "done";
  const iconColor = msg.toolStatus === "confirming" ? "var(--warning)" : msg.toolStatus === "running" ? "var(--accent)" : msg.toolStatus === "error" ? "var(--danger)" : "var(--success)";
  const chevron = expanded ? "▾" : "▸";

  // Derived: whether result needs truncation
  const { truncated, displayContent, isMarkdown } = useMemo(() => {
    if (!msg.toolResult) return { truncated: false, displayContent: "", isMarkdown: false };
    const long = msg.toolResult.length > MAX_PREVIEW_CHARS;
    const isMd = msg.toolResult.includes("## 🔍") || msg.toolResult.includes("⚠️") || msg.toolResult.includes("✅");
    return {
      truncated: long,
      displayContent: long && !fullExpanded
        ? msg.toolResult.slice(0, MAX_PREVIEW_CHARS)
        : msg.toolResult,
      isMarkdown: isMd,
    };
  }, [msg.toolResult, fullExpanded]);

  // Only render result when expanded
  const renderedResult = useMemo(() => {
    if (!expanded || !msg.toolResult) return null;
    return (
      <div className="tool-call-result">
        {isMarkdown ? (
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              a({ href, children, ...props }) {
                return (
                  <a
                    href={href}
                    onClick={(e) => {
                      e.preventDefault();
                      if (href) openUrl(href);
                    }}
                    style={{ color: "#2563eb", cursor: "pointer", textDecoration: "underline" }}
                    {...props}
                  >
                    {children}
                  </a>
                );
              },
            }}
          >
            {displayContent}
          </ReactMarkdown>
        ) : (
          <pre>{displayContent}</pre>
        )}
        {truncated && (
          <div style={{ marginTop: 8, fontSize: 11, color: "var(--text-muted)", textAlign: "center" }}>
            {fullExpanded ? (
              <span>{t("tool.show_all", { count: msg.toolResult.length })} ·{" "}
                <button className="btn btn-sm btn-ghost" style={{ fontSize: 11 }}
                  onClick={(e) => { e.stopPropagation(); setFullExpanded(false); }}>{t("tool.collapse")}</button>
              </span>
            ) : (
              <span>{t("tool.truncated", { count: MAX_PREVIEW_CHARS, total: msg.toolResult.length })} ·{" "}
                <button className="btn btn-sm btn-ghost" style={{ fontSize: 11 }}
                  onClick={(e) => { e.stopPropagation(); setFullExpanded(true); }}>{t("tool.expand")}</button>
              </span>
            )}
          </div>
        )}
      </div>
    );
  }, [expanded, msg.toolResult, displayContent, isMarkdown, truncated, fullExpanded, t]);

  return (
    <div className={`tool-call ${statusClass}`}>
      <div className="tool-call-header" onClick={() => setExpanded(!expanded)}>
        <span style={{ color: iconColor }}>◆</span>
        <span className="tool-call-name">{msg.toolName}</span>
        <span className="tool-call-args">{formatToolArgs(msg.toolArgs)}</span>
        <span className="tool-call-chevron">{chevron}</span>
      </div>
      {msg.toolStatus === "confirming" && onConfirm && (
        <div className="tool-call-confirm">
          <span className="tool-call-confirm-text">{t("tool.confirm_text")}</span>
          <div className="tool-call-confirm-actions">
            <button className="btn-allow" onClick={(e) => { e.stopPropagation(); onConfirm(msg.callId!, true); }}>{t("tool.allow")}</button>
            <button className="btn-deny" onClick={(e) => { e.stopPropagation(); onConfirm(msg.callId!, false); }}>{t("tool.deny")}</button>
          </div>
        </div>
      )}
      {renderedResult}
    </div>
  );
});

export default ToolCallBubble;
