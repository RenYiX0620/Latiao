import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "../i18n";
import { authFetch } from "../utils/api";

/**
 * 快捷调用 —— UI 与 Agent 共用同一份工具实现（2026-09-29 Action 同源）。
 *
 * 按钮不自己写业务逻辑：一律 POST /v1/tools/invoke → 后端 execute_tool，
 * 与 AI 调用同一条代码路径；结果写入 tool_calls 台账，下方「最近执行」
 * 轮询同一张表，所以按钮干的活 AI 能看见、AI 干的活按钮也能看见。
 */

interface ToolInfo {
  name: string;
  description: string;
  parameters?: { properties?: Record<string, unknown>; required?: string[] };
  permission: string;
}

interface RecentCall {
  id: string | number;
  tool_name: string;
  args: string;
  result: string;
  created_at: string;
}

const PRESETS: Record<string, string> = {
  list_dir: JSON.stringify({ path: "." }, null, 2),
  ak_finance: JSON.stringify({ query: "上证指数" }, null, 2),
  create_cron: JSON.stringify({ schedule: "0 9 * * *", task: "早上好，汇报今日待办" }, null, 2),
  run_cmd: JSON.stringify({ command: "echo hello" }, null, 2),
  read_file: JSON.stringify({ path: "README.md" }, null, 2),
};

export default function QuickCall({ showToast }: { showToast?: (m: string) => void }) {
  const { t } = useTranslation();
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [name, setName] = useState("");
  const [argsText, setArgsText] = useState("{}");
  const [busy, setBusy] = useState(false);
  const [lastResult, setLastResult] = useState<string>("");
  const [recent, setRecent] = useState<RecentCall[]>([]);
  const [needConfirm, setNeedConfirm] = useState(false);

  const tool = useMemo(() => tools.find((x) => x.name === name), [tools, name]);

  const loadTools = useCallback(async () => {
    try {
      const resp = await authFetch("/v1/tools");
      const data = await resp.json();
      if (data.status === "ok") {
        const list: ToolInfo[] = data.tools || [];
        setTools(list);
        if (!name && list.length) {
          const first = list.find((x) => x.name === "ak_finance") || list[0];
          setName(first.name);
          setArgsText(PRESETS[first.name] || "{}");
        }
      }
    } catch { /* 首屏静默 */ }
  }, [name]);

  const loadRecent = useCallback(async () => {
    try {
      const resp = await authFetch("/v1/memory/recent?limit=8");
      const data = await resp.json();
      if (data.status === "ok") setRecent(data.records || []);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    loadTools();
    loadRecent();
    const id = setInterval(loadRecent, 4000);
    return () => clearInterval(id);
  }, [loadTools, loadRecent]);

  async function run(confirmed: boolean) {
    if (!name) return;
    let args: unknown;
    try {
      args = JSON.parse(argsText || "{}");
    } catch {
      showToast?.(t("quick.args") + " JSON 无效");
      return;
    }
    setBusy(true);
    setNeedConfirm(false);
    try {
      const resp = await authFetch("/v1/tools/invoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, args: args as Record<string, unknown>, confirmed }),
      });
      const data = await resp.json();
      if (data.status === "need_confirm") {
        setNeedConfirm(true);
        showToast?.(t("quick.need_confirm"));
      } else if (data.status === "ok") {
        setLastResult(String(data.result ?? ""));
        showToast?.(t("quick.run") + " ✓");
        loadRecent();
      } else {
        setLastResult(String(data.message || data.result || "error"));
        showToast?.(String(data.message || "error"));
      }
    } catch (e) {
      setLastResult(String(e));
      showToast?.(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="card-title" style={{ marginBottom: 4 }}>{t("quick.title")}</div>
      <div className="card-desc" style={{ marginBottom: 10, fontSize: 11 }}>{t("quick.desc")}</div>

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <select
          className="text-input"
          value={name}
          onChange={(e) => {
            const v = e.target.value;
            setName(v);
            setArgsText(PRESETS[v] || "{}");
            setNeedConfirm(false);
          }}
        >
          {tools.map((x) => (
            <option key={x.name} value={x.name}>
              {x.name}{x.permission === "confirm" ? " ·⚠" : ""} — {(x.description || "").slice(0, 40)}
            </option>
          ))}
        </select>

        <textarea
          className="text-input"
          rows={5}
          style={{ fontFamily: "var(--font-mono, ui-monospace, monospace)", fontSize: 12, resize: "vertical" }}
          value={argsText}
          onChange={(e) => setArgsText(e.target.value)}
          spellCheck={false}
        />

        <div style={{ display: "flex", gap: 8 }}>
          {needConfirm || tool?.permission === "confirm" ? (
            <button className="btn btn-sm btn-primary" disabled={busy} onClick={() => run(true)}>
              {busy ? t("quick.running") : t("quick.confirm_run")}
            </button>
          ) : (
            <button className="btn btn-sm btn-primary" disabled={busy} onClick={() => run(false)}>
              {busy ? t("quick.running") : t("quick.run")}
            </button>
          )}
          <button className="btn btn-sm btn-ghost" onClick={() => setArgsText("{}")}>{"{}"}</button>
        </div>

        {lastResult && (
          <pre style={{
            background: "var(--bg-secondary, #f5f5f4)", border: "1px solid var(--border-default, #e5e5e5)",
            borderRadius: 8, padding: 10, fontSize: 11, maxHeight: 180, overflow: "auto", whiteSpace: "pre-wrap",
          }}>{lastResult.slice(0, 4000)}</pre>
        )}
      </div>

      <div style={{ marginTop: 14 }}>
        <div className="card-title" style={{ marginBottom: 6, fontSize: 12 }}>{t("quick.recent")}</div>
        {recent.length === 0 && <div className="card-desc">{t("quick.empty")}</div>}
        {recent.map((r) => (
          <div key={String(r.id)} style={{
            display: "flex", gap: 8, alignItems: "baseline", padding: "4px 0",
            borderBottom: "1px solid var(--border-default, #eee)", fontSize: 11,
          }}>
            <code style={{ fontWeight: 600 }}>{r.tool_name}</code>
            <span style={{ color: "var(--text-muted, #888)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {(r.args || "").slice(0, 80)}
            </span>
            <span style={{ color: "var(--text-muted, #888)", flexShrink: 0 }}>
              {(r.created_at || "").slice(11, 19)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
