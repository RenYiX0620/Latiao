import { useTranslation } from "../i18n";
import type { Message } from "../types";

/** 工具参数单行预览（取第一个键值对，超长截断）。 */
export function formatToolArgs(args?: Record<string, unknown>): string {
  if (!args) return "";
  const entries = Object.entries(args);
  if (entries.length === 0) return "";
  const [key, value] = entries[0];
  const valStr = typeof value === "string" ? value : JSON.stringify(value);
  return `${key}: ${valStr.length > 50 ? valStr.slice(0, 50) + "..." : valStr}`;
}

/**
 * 工具执行确认卡（允许一次 / 总是允许 / 拒绝）。
 *
 * 2026-10-05 从 ToolCallBubble 抽出，两处共用同一实现：
 * ① docked —— ChatView 停在**输入栏正上方**（默认形态）：等确认时用户视线就在
 *    输入栏，卡片不会像转录区那样被卷出屏幕外（对齐 ZCode 的权限面板形态）；
 * ② inline —— 转录区气泡内（历史记录形态，docked 缺失时的兜底）。
 * 两处行为严格一致，含"本会话已总是允许"的撤销入口。
 */
export function ToolConfirmCard({ msg, onConfirm, sessionId, docked = false }: {
  msg: Message;
  sessionId?: string;
  docked?: boolean;
  onConfirm: (callId: string, approved: boolean, always?: boolean, toolName?: string, sessionId?: string) => void;
}) {
  const { t } = useTranslation();
  const alwaysAllowed = (() => {
    try {
      const keys = Object.keys(localStorage).filter((k) => k.startsWith("latiao_always_allow"));
      for (const k of keys) {
        const arr: string[] = JSON.parse(localStorage.getItem(k) || "[]");
        if (msg.toolName && arr.includes(msg.toolName)) return true;
      }
      return false;
    } catch { return false; }
  })();

  return (
    <div className={`tool-call-confirm${docked ? " is-docked" : ""}`} role="alert">
      <div className="tool-call-confirm-left">
        <span className="tool-call-confirm-badge">?</span>
        <div className="tool-call-confirm-copy">
          <div className="tool-call-confirm-title">{t("tool.confirm_title", { tool: msg.toolName || "tool" })}</div>
          <div className="tool-call-confirm-sub">{formatToolArgs(msg.toolArgs) || t("tool.confirm_text")}</div>
          {alwaysAllowed && (
            <button
              type="button"
              className="tool-call-confirm-warn"
              onClick={(e) => {
                e.stopPropagation();
                try {
                  for (const k of Object.keys(localStorage).filter((k) => k.startsWith("latiao_always_allow"))) {
                    const arr: string[] = JSON.parse(localStorage.getItem(k) || "[]");
                    const next = arr.filter((n) => n !== msg.toolName);
                    if (next.length !== arr.length) localStorage.setItem(k, JSON.stringify(next));
                  }
                } catch { /* ignore */ }
              }}
            >{t("tool.will_auto_allow")}</button>
          )}
        </div>
      </div>
      <div className="tool-call-confirm-actions">
        <button className="btn-allow" onClick={(e) => { e.stopPropagation(); onConfirm(msg.callId!, true); }}>{t("tool.allow_once")}</button>
        <button className="btn-always" onClick={(e) => { e.stopPropagation(); onConfirm(msg.callId!, true, true, msg.toolName, sessionId); }}>{t("tool.allow_always")}</button>
        <button className="btn-deny" onClick={(e) => { e.stopPropagation(); onConfirm(msg.callId!, false); }}>{t("tool.deny")}</button>
      </div>
    </div>
  );
}

export default ToolConfirmCard;
