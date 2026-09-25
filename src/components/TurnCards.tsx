/**
 * 回合结果卡（2026-09-24，对齐 MiMo Desktop 信息架构）：
 * - ToolActivityCard：过程 —— 「已读取 N 个文件 · 已运行 N 条命令」折叠明细
 * - FileEditCard：结果 —— 「已编辑 N 个文件 +Δ −Δ」+ 撤销 / 审阅
 */
import { memo, useMemo, useState } from "react";
import {
  ChevronDown, ChevronRight, FilePen, FileText, FolderOpen, Terminal,
  Search, Wrench, Undo2, FileSearch, Loader2, Database, Users, Clock,
  AppWindow, Camera, ListTree, Play, History, ScanLine, MousePointer2, Keyboard,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { Message } from "../types";
import { useTranslation } from "../i18n";
import type { PreviewItem } from "./PreviewPanel";

const KIND_ICONS: Record<string, LucideIcon> = {
  bing_search: Search, web_search: Search, tavily_search: Search, search_files: Search,
  mx_query: Database, ak_finance: Database,
  write_file: FilePen, read_file: FileText,
  list_dir: FolderOpen, open_folder: FolderOpen,
  open_app: AppWindow, run_cmd: Terminal,
  delegate_task: Users, create_cron: Clock,
  screen_capture: Camera, control_list_processes: ListTree,
  control_kill_process: ScanLine, control_launch: Play,
  control_process_log: FileText, control_mouse_move: MousePointer2,
  control_mouse_click: MousePointer2, control_keyboard_type: Keyboard,
  control_keyboard_press: Keyboard, control_wait: Clock, control_audit: History,
};

type Kind = "read" | "exec" | "write" | "search" | "other";

function kindOf(toolName: string): Kind {
  if (["read_file", "list_dir", "control_process_log"].includes(toolName)) return "read";
  if (["run_cmd", "control_launch"].includes(toolName)) return "exec";
  if (toolName === "write_file") return "write";
  if (["web_search", "bing_search", "tavily_search", "search_files"].includes(toolName)) return "search";
  return "other";
}

function argSummary(m: Message): string {
  const a = (m.toolArgs || {}) as Record<string, unknown>;
  const pick = (...ks: string[]) => {
    for (const k of ks) {
      const v = a[k];
      if (typeof v === "string" && v) return v;
      if (typeof v === "number") return String(v);
    }
    return "";
  };
  const tool = m.toolName || "";
  let s: string;
  if (tool === "run_cmd" || tool === "control_launch") s = pick("command", "description");
  else if (kindOf(tool) === "search") s = pick("query", "pattern", "url");
  else if (["read_file", "write_file", "list_dir", "open_folder", "open_app", "control_process_log"].includes(tool))
    s = pick("path", "directory", "app", "pattern");
  else s = pick("query", "index", "name", "task");
  if (!s) s = JSON.stringify(a || {}).slice(0, 60);
  return s.replace(/\s+/g, " ").trim().slice(0, 80);
}

function shortPath(p: string): string {
  const parts = p.replace(/\\/g, "/").split("/").filter(Boolean);
  return parts.slice(-3).join("/");
}

/** write_file 行数增删：新增内容行数记 +；无旧快照时 − 记 0。 */
export function editStats(m: Message): { path: string; added: number; removed: number; content: string } {
  const a = (m.toolArgs || {}) as Record<string, unknown>;
  const path = String(a.path || a.file || a.name || "");
  const content = typeof a.content === "string" ? a.content : "";
  const added = content ? content.split("\n").length : 0;
  return { path, added, removed: 0, content };
}

export const FileEditCard = memo(function FileEditCard({ msgs, onUndo, onPreview }: {
  msgs: Message[];
  onUndo?: () => void;
  onPreview?: (item: PreviewItem) => void;
}) {
  const { t } = useTranslation();
  const [showAll, setShowAll] = useState(false);
  const [reviewPath, setReviewPath] = useState<string | null>(null);
  const edits = useMemo(() => {
    const all = msgs.filter((m) => m.toolStatus === "done" && m.toolName === "write_file").map(editStats);
    // 生成器脚本 `x.docx.py` + 真文档 `x.docx` 会同时出现在写文件里——
    // 用户只要交付物：有真文档就丢掉同名脚本
    const real = new Set(all.map((e) => e.path));
    return all
      .filter((e) => {
        const m = e.path.match(/\.(docx?|xlsx?|pptx?|pdf)\.py$/i);
        return !m || !real.has(e.path.replace(/\.py$/i, ""));
      })
      .map((e) => {
        // 只剩生成器时：卡片改展示猜出来的真文档（预览栏同样会找它）
        const m = e.path.match(/\.(docx?|xlsx?|pptx?|pdf)\.py$/i);
        if (!m) return e;
        const deliverable = e.path.replace(/\.py$/i, "");
        return { ...e, path: deliverable, content: e.content };
      });
  }, [msgs]);
  const totalAdd = edits.reduce((s, e) => s + e.added, 0);
  const totalDel = edits.reduce((s, e) => s + e.removed, 0);
  const VISIBLE = 3;
  const shown = showAll ? edits : edits.slice(0, VISIBLE);
  const rest = Math.max(0, edits.length - VISIBLE);
  const review = edits.find((e) => e.path === reviewPath);

  return (
    <div className="edit-card">
      <div className="edit-card-head">
        <span className="edit-card-icon"><FilePen size={16} /></span>
        <div className="edit-card-title">
          <div className="edit-card-label">{t("cards.edited_n", { n: edits.length })}</div>
          <div className="edit-card-delta">
            <span className="delta-add">+{totalAdd}</span>
            <span className="delta-del">−{totalDel}</span>
          </div>
        </div>
        <div className="edit-card-actions">
          <button className="btn btn-sm btn-ghost" onClick={onUndo}>
            <Undo2 size={13} /> {t("cards.undo")}
          </button>
          <button className="btn btn-sm btn-primary" onClick={() => {
            const first = edits[0];
            if (first && onPreview) onPreview({ name: first.path.split(/[\\/]/).pop() || first.path, path: first.path, content: first.content });
            else setReviewPath(reviewPath ? null : edits[0]?.path || null);
          }}>
            <FileSearch size={13} /> {t("cards.review")}
          </button>
        </div>
      </div>
      <div className="edit-card-body">
        {shown.map((e) => (
          <div
            key={e.path || e.content.slice(0, 20)}
            className="edit-card-row clickable"
            title={e.path}
            onClick={() => onPreview && onPreview({
              name: e.path.split(/[\\/]/).pop() || e.path,
              path: e.path,
              content: e.content,
            })}
          >
            <span className="edit-card-path">{shortPath(e.path) || t("cards.file")}</span>
            <span className="edit-card-stat">
              <span className="delta-add">+{e.added}</span>
              <span className="delta-del">−{e.removed}</span>
            </span>
          </div>
        ))}
        {rest > 0 && !showAll && (
          <button className="edit-card-more" onClick={() => setShowAll(true)}>
            {t("cards.more_files", { n: rest })} <ChevronDown size={12} />
          </button>
        )}
        {review && (
          <pre className="edit-card-review">{review.content.slice(0, 4000)}</pre>
        )}
      </div>
    </div>
  );
});

export const SubagentRow = memo(function SubagentRow({ agent, task, status, onClick }: {
  agent: string;
  task: string;
  status?: string;
  onClick?: () => void;
}) {
  const { t } = useTranslation();
  const isExplore = (agent || "").toLowerCase().includes("explore");
  const Icon = isExplore ? Search : Users;
  const agentLabel = agent ? agent[0].toUpperCase() + agent.slice(1) : "Agent";
  return (
    <div
      className={`subagent-line${status === "running" ? " running" : status === "error" ? " error" : ""}`}
      onClick={onClick}
      title={task}
    >
      <span className="subagent-line-icon"><Icon size={14} /></span>
      <span className="subagent-line-label">{t("cards.subagent")}</span>
      <span className="subagent-line-agent">{agentLabel}</span>
      <span className="subagent-line-task">· {task.slice(0, 80)}</span>
    </div>
  );
});

export const ToolActivityCard = memo(function ToolActivityCard({ msgs }: { msgs: Message[] }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [openRow, setOpenRow] = useState<string | null>(null);

  const counts = useMemo(() => {
    const c: Record<Kind, number> = { read: 0, exec: 0, write: 0, search: 0, other: 0 };
    for (const m of msgs) c[kindOf(m.toolName || "")] += 1;
    return c;
  }, [msgs]);

  const parts: string[] = [];
  if (counts.read) parts.push(t("cards.did_read", { n: counts.read }));
  if (counts.exec) parts.push(t("cards.did_exec", { n: counts.exec }));
  if (counts.write) parts.push(t("cards.did_write", { n: counts.write }));
  if (counts.search) parts.push(t("cards.did_search", { n: counts.search }));
  if (counts.other) parts.push(t("cards.did_other", { n: counts.other }));
  const summary = parts.join(" · ");

  return (
    <div className="activity-card">
      <button className="activity-card-head" onClick={() => setOpen(!open)}>
        <span className="activity-card-icon"><ListTree size={15} /></span>
        <span className="activity-card-label">{t("cards.activity")}</span>
        <span className="activity-card-meta">{summary}</span>
        <span className="activity-card-chevron">{open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</span>
      </button>
      {open && (
        <div className="activity-card-body">
          {msgs.map((m) => {
            const Icon = KIND_ICONS[m.toolName || ""] || Wrench;
            const rowKey = m.id || m.callId || argSummary(m);
            const isOpen = openRow === rowKey;
            return (
              <div key={rowKey} className="activity-row">
                <button className="activity-row-head" onClick={() => setOpenRow(isOpen ? null : rowKey)}>
                  <Icon size={13} />
                  <span className="activity-row-kind">
                    {kindOf(m.toolName || "") === "read" ? t("cards.did_read", { n: 1 }).replace(/[0-9]+\s*/, "")
                      : kindOf(m.toolName || "") === "exec" ? t("cards.did_exec", { n: 1 }).replace(/[0-9]+\s*/, "")
                      : kindOf(m.toolName || "") === "write" ? t("cards.did_write", { n: 1 }).replace(/[0-9]+\s*/, "")
                      : m.toolName}
                  </span>
                  <span className="activity-row-sum">{argSummary(m)}</span>
                  {m.toolStatus === "running" && <Loader2 size={12} className="tool-call-spinner" />}
                  {isOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                </button>
                {isOpen && m.toolResult && <pre className="activity-row-result">{m.toolResult.slice(0, 2000)}</pre>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
});
