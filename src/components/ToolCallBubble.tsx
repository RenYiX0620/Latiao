import { useState, useMemo, memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useTranslation } from "../i18n";
import { openUrl } from "@tauri-apps/plugin-opener";
import {
  Search, Database, LineChart, FilePen, FileText, FolderOpen, AppWindow,
  Terminal, FileSearch, Users, Clock, Wrench, ChevronRight, ChevronDown, Loader2,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { Message } from "../types";

const MAX_PREVIEW_CHARS = 2000;

// 每个工具一个语义图标（lucide 线性风格），未知工具回退到扳手
export const TOOL_ICONS: Record<string, LucideIcon> = {
  bing_search: Search, web_search: Search, tavily_search: Search,
  mx_query: Database, ak_finance: LineChart,
  write_file: FilePen, read_file: FileText,
  list_dir: FolderOpen, open_folder: FolderOpen,
  open_app: AppWindow, run_cmd: Terminal, search_files: FileSearch,
  delegate_task: Users, create_cron: Clock,
};

function formatToolArgs(args?: Record<string, unknown>): string {
  if (!args) return "";
  const entries = Object.entries(args);
  if (entries.length === 0) return "";
  const [key, value] = entries[0];
  const valStr = typeof value === "string" ? value : JSON.stringify(value);
  return `${key}: ${valStr.length > 50 ? valStr.slice(0, 50) + "..." : valStr}`;
}

function fmtDur(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s} 秒`;
  return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
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
  const ToolIcon = TOOL_ICONS[msg.toolName || ""] || Wrench;

  // Derived: whether result needs truncation
  const { truncated, displayContent, isMarkdown } = useMemo(() => {
    if (!msg.toolResult) return { truncated: false, displayContent: "", isMarkdown: false };
    const long = msg.toolResult.length > MAX_PREVIEW_CHARS;
    const isMd = msg.toolResult.includes("## 🔍") || msg.toolResult.includes("⚠️") || msg.toolResult.includes("✅")
      || /[|]/.test(msg.toolResult) || msg.toolResult.startsWith("|");  // markdown 表格
    return {
      truncated: long,
      displayContent: long && !fullExpanded
        ? msg.toolResult.slice(0, MAX_PREVIEW_CHARS)
        : msg.toolResult,
      isMarkdown: isMd,
      isError: msg.toolResult.startsWith("Error") || msg.toolResult.startsWith("⛔"),
    };
  }, [msg.toolResult, fullExpanded]);

  // Only render result when expanded
  const renderedResult = useMemo(() => {
    if (!msg.toolResult) return null;
    if (!expanded) {
      // ZCode 风格：默认完全折叠成一行（头部），点击才展开结果
      return null;
    }
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
                      if (href) openUrl(href).catch(() => {});
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
        <span className="tool-call-icon" style={{ color: iconColor }}><ToolIcon size={14} /></span>
        <span className="tool-call-name">{msg.toolName}</span>
        <span className="tool-call-args">{formatToolArgs(msg.toolArgs)}</span>
        {msg.duration !== undefined && msg.toolStatus === "done" && (
          <span className="tool-call-duration">· {fmtDur(msg.duration)}</span>
        )}
        {msg.toolStatus === "running" && <Loader2 size={13} className="tool-call-spinner" />}
        <span className="tool-call-chevron">{expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</span>
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
