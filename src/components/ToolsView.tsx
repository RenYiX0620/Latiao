import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "../i18n";
import { authFetch } from "../utils/api";


interface ExtensionInfo {
  name: string;
  version: string;
  description: string;
  description_i18n?: Record<string, string>;
  author?: { name?: string } | string;
  permissions: string[];
  enabled: boolean;
  source: string;
  installed_at: number;
  has_plugin: boolean;
  has_skills: boolean;
  has_agents: boolean;
}

interface ToolsViewProps {
  tools: { name: string; description: string; parameters: Record<string, unknown>; permission: string; usage_count: number }[];
  setTools: React.Dispatch<React.SetStateAction<{ name: string; description: string; parameters: Record<string, unknown>; permission: string; usage_count: number }[]>>;
  showToast: (msg: string, kind?: "warn") => void;
}

const PERM_LABEL: Record<string, string> = {
  readonly: "只读",
  files: "文件读写",
  network: "网络",
  shell: "命令执行",
};

export default function ToolsView({ tools, setTools, showToast }: ToolsViewProps) {
  const { t } = useTranslation();
  const iconMap: Record<string, string> = { read_file: "📄", write_file: "✏️", list_dir: "📁", run_cmd: "⚡", open_folder: "📂", open_app: "🚀", search_files: "🔍" };

  // ── 扩展页（Latiao 扩展市场体系：无缝安装） ──
  const [extensions, setExtensions] = useState<ExtensionInfo[]>([]);
  const [installSrc, setInstallSrc] = useState("");
  const [installing, setInstalling] = useState(false);
  const [confirming, setConfirming] = useState<{ source: string; sha256: string; permissions: string[] } | null>(null);
  // ── 市场（Phase 2a） ──
  const [marketTab, setMarketTab] = useState<"market" | "installed">("market");
  const [marketPlugins, setMarketPlugins] = useState<any[]>([]);
  const [marketLoading, setMarketLoading] = useState(false);
  const [marketErr, setMarketErr] = useState("");

  const refreshMarket = useCallback(async () => {
    setMarketLoading(true);
    setMarketErr("");
    try {
      const resp = await authFetch("/v1/marketplace?url=");
      const data = await resp.json();
      if (data.status === "ok") setMarketPlugins(data.plugins || []);
      else setMarketErr(data.message || "市场加载失败");
    } catch (e) { console.error("市场加载失败:", e); setMarketErr("市场加载失败（" + String((e as Error)?.message || e) + "）"); }
    finally { setMarketLoading(false); }
  }, []);

  useEffect(() => { refreshMarket(); }, [refreshMarket]);

  const refreshExtensions = useCallback(async () => {
    try {
      const resp = await authFetch("/v1/extensions");
      const data = await resp.json();
      if (data.status === "ok") setExtensions(data.extensions || []);
    } catch { /* 静默：扩展页不可用不影响工具页 */ }
  }, []);

  useEffect(() => { refreshExtensions(); }, [refreshExtensions]);

  const doInstall = async (source: string, sha256: string) => {
    setInstalling(true);
    try {
      const resp = await authFetch("/v1/extensions/install", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source, sha256 }),
      });
      const data = await resp.json();
      if (data.status === "ok") {
        showToast(data.message || "已安装");
        setInstallSrc("");
        refreshExtensions();
      } else {
        showToast(data.message || "安装失败", "warn");
      }
    } catch (e) { console.error(e); showToast("安装请求失败", "warn"); }
    finally { setInstalling(false); setConfirming(null); }
  };

  // 两步安装：先读 manifest 权限 → 弹确认 → 真正安装
  const preflightInstall = async (source: string) => {
    if (!source.trim()) return;
    // 后端安装接口在确认前先探一下权限：调用 install 会直接装上，
    // 所以先请求权限预检（后端 install 返回 permissions 后再确认会太晚）。
    // 简化方案：本地/URL 直接询问用户是否信任该来源。
    setConfirming({ source: source.trim(), sha256: "", permissions: [] });
  };

  const installFromMarket = async (item: any) => {
    setConfirming({ source: item.source_url || "", sha256: item.sha256 || "", permissions: [] });
  };

  const toggleExt = async (ext: ExtensionInfo) => {
    try {
      const resp = await authFetch("/v1/extensions/set-enabled", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: ext.name, enabled: !ext.enabled }),
      });
      const data = await resp.json();
      if (data.status !== "ok") { showToast(data.message || "操作失败", "warn"); return; }
      refreshExtensions();
    } catch (e) { console.error(e); showToast("操作失败", "warn"); }
  };

  const uninstallExt = async (ext: ExtensionInfo) => {
    try {
      const resp = await authFetch("/v1/extensions/uninstall", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: ext.name }),
      });
      const data = await resp.json();
      if (data.status !== "ok") { showToast(data.message || "卸载失败", "warn"); return; }
      showToast(data.message || "已卸载");
      refreshExtensions();
    } catch (e) { console.error(e); showToast("卸载失败", "warn"); }
  };

  const authorName = (a: ExtensionInfo["author"]) =>
    typeof a === "string" ? a : (a?.name || "");

  return (
    <div>
      {/* ═══ 扩展市场 ═══ */}
      <div className="page-header" style={{ marginBottom: 12 }}>
        <div>
          <div className="card-title" style={{ fontSize: 15 }}>🧩 扩展</div>
          <div className="card-desc" style={{ marginTop: 2 }}>
            无缝安装：拖入 .latiaoext 文件、粘贴 URL 或 GitHub 仓库地址即可安装工具插件、技能与子智能体组合包
          </div>
        </div>
      </div>

      {/* 市场 / 已装 tab */}
      <div className="tab-bar" style={{ marginBottom: 12 }}>
        <button className={`tab-btn${marketTab === "market" ? " active" : ""}`}
          onClick={() => setMarketTab("market")}>市场</button>
        <button className={`tab-btn${marketTab === "installed" ? " active" : ""}`}
          onClick={() => setMarketTab("installed")}>已安装</button>
      </div>

      {marketTab === "market" && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="card-title" style={{ marginBottom: 8 }}>官方市场</div>
          {marketLoading && <div className="card-desc">加载中…</div>}
          {marketErr && <div className="card-desc" style={{ color: "var(--danger)" }}>{marketErr}</div>}
          {!marketLoading && !marketErr && marketPlugins.length === 0 && (
            <div className="card-desc">市场为空——等待官方扩展上架。已支持：粘贴 URL/GitHub 仓库/本地文件安装。</div>
          )}
          {marketPlugins.map((item) => (
            <div key={item.name} style={{
              display: "flex", alignItems: "center", gap: 10, padding: "8px 0",
              borderBottom: "1px solid var(--border-default)",
            }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ fontWeight: 650, fontSize: 13 }}>🧩 {item.name}</span>
                  <span className="badge badge-safe">v{item.version}</span>
                  {item.update_available && <span className="badge badge-confirm">有更新</span>}
                </div>
                <div className="card-desc" style={{ marginTop: 2 }}>{item.description}</div>
                <div className="card-meta" style={{ marginTop: 2 }}>
                  {typeof item.author === "object" && item.author ? item.author.name || "" : ""}
                  {item.category ? ` · ${item.category}` : ""}
                </div>
              </div>
              <button className={`btn btn-sm ${item.installed ? "btn-ghost" : "btn-primary"}`}
                disabled={item.installed && !item.update_available}
                onClick={() => item.installed && item.update_available
                  ? setConfirming({ source: item.source_url, sha256: item.sha256, permissions: [] })
                  : installFromMarket(item)}>
                {item.update_available ? "更新" : item.installed ? "已安装" : "安装"}
              </button>
            </div>
          ))}
        </div>
      )}

      {marketTab === "installed" && (
      <>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-title" style={{ marginBottom: 8 }}>安装扩展</div>
        <div style={{ display: "flex", gap: 8 }}>
          <input
            className="text-input"
            style={{ flex: 1 }}
            placeholder="粘贴 URL / GitHub 仓库 / .latiaoext 文件路径"
            value={installSrc}
            onChange={(e) => setInstallSrc(e.target.value)}
          />
          <button className="btn btn-primary" disabled={installing || !installSrc.trim()}
            onClick={() => preflightInstall(installSrc)}>
            {installing ? "安装中…" : "安装"}
          </button>
        </div>
        <div className="card-desc" style={{ marginTop: 6 }}>
          支持：https://github.com/owner/repo（或 /tree/分支/子目录）、zip 直链、本地文件路径
        </div>
      </div>

      {/* 权限确认弹层 */}
      {confirming && (
        <div className="card" style={{ marginBottom: 14, borderLeft: "2px solid var(--warning)" }}>
          <div className="card-title">⚠️ 确认安装来源</div>
          <div className="card-desc" style={{ marginTop: 4, wordBreak: "break-all" }}>
            {confirming.source}
          </div>
          <div className="card-desc" style={{ marginTop: 4 }}>
            扩展包将获得其 manifest 声明的权限（只读/文件/网络/命令）。安装前请确认来源可信。
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            <button className="btn btn-primary" disabled={installing}
              onClick={() => doInstall(confirming.source, confirming.sha256)}>确认安装</button>
            <button className="btn btn-ghost" onClick={() => setConfirming(null)}>取消</button>
          </div>
        </div>
      )}

      <div className="card-grid">
        {extensions.map((ext) => (
          <div key={ext.name} className="card" style={ext.enabled ? {} : { opacity: 0.55 }}>
            <div className="card-title">
              🧩 {ext.name}
              <span className="badge badge-safe">v{ext.version}</span>
              {!ext.enabled && <span className="badge badge-confirm">已禁用</span>}
            </div>
            <div className="card-desc">{ext.description}</div>
            <div className="card-meta" style={{ marginTop: 6 }}>
              <span>{authorName(ext.author) || "未知作者"}</span>
              <span> · </span>
              <span>{(ext.permissions || []).map((p) => PERM_LABEL[p] || p).join(" / ") || "只读"}</span>
            </div>
            <div className="card-meta" style={{ marginTop: 4 }}>
              <span>📦 {ext.has_plugin ? "插件" : ""}{ext.has_skills ? " 技能" : ""}{ext.has_agents ? " 子智能体" : ""}</span>
            </div>
            <div style={{ display: "flex", gap: 6, marginTop: 10 }}>
              <button className={`btn btn-sm ${ext.enabled ? "btn-ghost" : "btn-primary"}`}
                onClick={() => toggleExt(ext)}>{ext.enabled ? "禁用" : "启用"}</button>
              <button className="btn btn-sm btn-ghost" onClick={() => uninstallExt(ext)}>卸载</button>
            </div>
          </div>
        ))}
        {extensions.length === 0 && (
          <div className="card" style={{ gridColumn: "1 / -1" }}>
            <div className="card-desc">
              还没有安装扩展。上方粘贴来源即可无缝安装（插件 + 技能 + 子智能体组合包）。
            </div>
          </div>
        )}
      </div>
      </>
      )}

      {/* ═══ 工具列表（原有内容） ═══ */}
      <div className="page-header" style={{ margin: "18px 0 12px" }}>
        <div className="card-title" style={{ fontSize: 15 }}>🔧 工具</div>
      </div>
      <div className="card-grid">
        {tools.map((tool) => {
          const isSafe = tool.permission === "safe";
          return (
          <div key={tool.name} className="card" style={isSafe ? {} : { borderLeft: "2px solid var(--warning)" }}>
            <div className="card-title">{iconMap[tool.name] || "🔧"} {tool.name}
              <span className={`badge ${isSafe ? "badge-safe" : "badge-confirm"}`}>{isSafe ? t("tools.safe") : t("tools.confirm")}</span>
            </div>
            <div className="card-desc">{tool.description}</div>
            <div className="card-meta" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span>{t("tools.calls", { count: tool.usage_count })}</span>
              <span style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                {Object.keys(tool.parameters?.properties || {}).length > 0
                  ? Object.keys((tool.parameters as Record<string, unknown>)?.properties as Record<string, unknown> || {}).join(", ")
                  : t("tools.no_params")}
              </span>
            </div>
            <button className={`btn btn-sm ${isSafe ? "btn-ghost" : "btn-primary"}`} style={{ marginTop: 10, width: "100%" }}
              onClick={async (e) => {
                e.stopPropagation();
                const newPerm = isSafe ? "confirm" : "safe";
                try {
                  const resp = await authFetch("/v1/permissions", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ tool: tool.name, permission: newPerm }),
                  });
                  if (!resp.ok) { showToast(t("tools.toggle_fail"), "warn"); return; }
                  setTools(prev => prev.map(t2 => t2.name === tool.name ? { ...t2, permission: newPerm } : t2));
                  showToast(`${tool.name} → ${newPerm === "safe" ? t("tools.safe") : t("tools.confirm")}`);
                } catch (e) { console.error(e); showToast(t("tools.toggle_fail"), "warn"); }
              }}>
              {isSafe ? t("tools.set_confirm") : t("tools.set_safe")}
            </button>
          </div>
        )})}
      </div>
    </div>
  );
}
