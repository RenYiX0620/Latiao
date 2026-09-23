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

// 统一能力模型条目（sidecar capabilities 表）
interface Capability {
  name: string;
  kind: "tool" | "skill";
  display_name: string;
  description: string;
  permission: string;
  enabled: boolean;
  source: string;
  usage_count: number;
}

interface ToolsViewProps {
  capabilities: Capability[];
  setCapabilities: React.Dispatch<React.SetStateAction<Capability[]>>;
  showToast: (msg: string, kind?: "warn") => void;
}

// 这两张表存**键**、用的时候再翻（模块级常量里拿不到组件内的 t）
const PERM_LABEL: Record<string, string> = {
  readonly: "perm.readonly",
  files: "perm.files",
  network: "perm.network",
  shell: "perm.shell",
};

const CAP_PERM_LABEL: Record<string, string> = {
  safe: "perm.safe",
  confirm: "perm.confirm",
  danger: "perm.danger",
  deny: "perm.deny",
};

export default function ToolsView({ capabilities, setCapabilities, showToast }: ToolsViewProps) {
  const { t } = useTranslation();

  // ── 扩展页（Latiao 扩展市场体系：无缝安装） ──
  const [extensions, setExtensions] = useState<ExtensionInfo[]>([]);
  const [installSrc, setInstallSrc] = useState("");
  const [installing, setInstalling] = useState(false);
  const [confirming, setConfirming] = useState<{ source: string; sha256: string; permissions: string[]; githubItem?: any; isGitHubItem?: boolean } | null>(null);
  // ── 市场 ──
  const [marketTab, setMarketTab] = useState<"market" | "installed">("market");
  const [marketPlugins, setMarketPlugins] = useState<any[]>([]);
  const [marketLoading, setMarketLoading] = useState(false);
  const [marketErr, setMarketErr] = useState("");
  // ── 多市场源（Phase 1） ──
  const [marketSources, setMarketSources] = useState<any[]>([]);
  const [blockedSources, setBlockedSources] = useState<string[]>([]);
  const [newSourceUrl, setNewSourceUrl] = useState("");
  const [showSourceForm, setShowSourceForm] = useState(false);
  // ── GitHub 自动发现（Discovery Engine） ──
  const [discoverState, setDiscoverState] = useState<any>({ last_scan_ts: 0, repos: 0, entries: 0 });
  const [discoverRefreshing, setDiscoverRefreshing] = useState(false);
  // ── 市场列表折叠（默认收起，890 条太长） ──
  const [marketCollapsed, setMarketCollapsed] = useState(false);
  // ── 市场搜索（890 条过滤，纯前端） ──
  const [marketQuery, setMarketQuery] = useState("");
  // ── 市场类别筛选（全部/官方/OpenClaw/Claude/GitHub发现） ──
  const [marketCat, setMarketCat] = useState<"all" | "official" | "openclaw" | "claude" | "discovered">("all");
  // ── 统一能力列表（工具与技能一个列表，不分栏） ──
  // ── 新建技能表单 ──
  const [newSkillName, setNewSkillName] = useState("");
  const [newSkillContent, setNewSkillContent] = useState("");
  const [skillFormOpen, setSkillFormOpen] = useState(false);
  // ── Tavily key 配置 ──
  const [tavilyKey, setTavilyKey] = useState({ hasKey: false, masked: null as string | null, loading: false });
  const [keyInput, setKeyInput] = useState("");
  const [showKeyInput, setShowKeyInput] = useState(false);

  const refreshMarket = useCallback(async () => {
    setMarketLoading(true);
    setMarketErr("");
    // 聚合所有源（官方 marketplace + 生态源发现）；瞬时抖动自动重试一次
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const resp = await authFetch("/v1/marketplace/all");
        const data = await resp.json();
        if (data.status === "ok") {
          setMarketPlugins(data.plugins || []);
          const errs = data.errors || [];
          setMarketErr(errs.length ? errs.join("；") : "");
          break;
        }
        setMarketErr(data.message || t("tools.market_fail"));
      } catch (e) {
        console.error("市场加载失败(尝试" + (attempt + 1) + "):", e);
        if (attempt === 0) { await new Promise(r => setTimeout(r, 800)); continue; }
        setMarketErr(t("tools.market_fail") + "（" + String((e as Error)?.message || e) + "）");
      }
    }
    setMarketLoading(false);
  }, []);

  const refreshSources = useCallback(async () => {
    try {
      const resp = await authFetch("/v1/marketplace/sources");
      const data = await resp.json();
      if (data.status === "ok") setMarketSources(data.sources || []);
      try {
        const bresp = await authFetch("/v1/extensions/blocked-sources");
        const bdata = await bresp.json();
        if (bdata.status === "ok") setBlockedSources(bdata.blocked || []);
      } catch { /* 黑名单不可用不影响源列表 */ }
    } catch { /* 静默 */ }
  }, []);

  const refreshDiscover = useCallback(async () => {
    try {
      const resp = await authFetch("/v1/marketplace/discover-status");
      const data = await resp.json();
      if (data.status === "ok") setDiscoverState(data);
    } catch { /* 静默 */ }
  }, []);

  const triggerDiscoverRefresh = async () => {
    setDiscoverRefreshing(true);
    try {
      const resp = await authFetch("/v1/marketplace/discover-refresh", { method: "POST" });
      const data = await resp.json();
      if (data.status === "ok") {
        showToast(data.message || t("tools.discover_started"));
        // 轮询状态直到完成（抓取 3-5 分钟）
        setTimeout(async () => { await refreshDiscover(); await refreshMarket(); setDiscoverRefreshing(false); }, 12000);
      } else { showToast(data.message || t("tools.refresh_fail"), "warn"); setDiscoverRefreshing(false); }
    } catch (e) { console.error(e); showToast(t("tools.refresh_fail"), "warn"); setDiscoverRefreshing(false); }
  };

  useEffect(() => { refreshMarket(); refreshSources(); refreshDiscover(); }, [refreshMarket, refreshSources, refreshDiscover]);

  const refreshExtensions = useCallback(async () => {
    try {
      const resp = await authFetch("/v1/extensions");
      const data = await resp.json();
      if (data.status === "ok") setExtensions(data.extensions || []);
    } catch { /* 静默：扩展页不可用不影响能力列表 */ }
  }, []);

  useEffect(() => { refreshExtensions(); }, [refreshExtensions]);

  // 扩展安装/卸载后，能力表也变了 → 同步刷新统一列表
  const refreshCapabilities = useCallback(async () => {
    try {
      const resp = await authFetch("/v1/capabilities");
      const data = await resp.json();
      if (data.status === "ok") setCapabilities(data.capabilities || []);
    } catch { /* 静默 */ }
  }, [setCapabilities]);

  // Tavily key 状态（tavily_search 能力存在时展示配置区）
  useEffect(() => {
    if (!capabilities.some(c => c.name === "tavily_search")) return;
    (async () => {
      try {
        const resp = await authFetch("/v1/settings/tavily-key");
        const data = await resp.json();
        if (data.status === "ok") setTavilyKey({ hasKey: data.has_key, masked: data.masked, loading: false });
      } catch { /* 静默 */ }
    })();
  }, [capabilities]);

  const doInstall = async (source: string, sha256: string) => {
    setInstalling(true);
    try {
      // 生态源条目：download+pack 后安装（install-github）；否则走现有 install
      const isGitHub = !!confirming?.isGitHubItem && confirming?.githubItem;
      const resp = isGitHub
        ? await authFetch("/v1/extensions/install-github", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              repo: confirming.githubItem.repo || confirming.githubItem.source_url,
              skill_path: confirming.githubItem.skill_path || "",
              kind: confirming.githubItem.source_kind || "openclaw-skill",
            }),
          })
        : await authFetch("/v1/extensions/install", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ source, sha256 }),
          });
      const data = await resp.json();
      if (data.status === "ok") {
        showToast(data.message || t("tools.installed"));
        setInstallSrc("");
        refreshExtensions();
        refreshCapabilities();
      } else {
        showToast(data.message || t("tools.install_fail"), "warn");
      }
    } catch (e) { console.error(e); showToast(t("tools.install_request_fail"), "warn"); }
    finally { setInstalling(false); setConfirming(null); }
  };

  // 两步安装：先读 manifest 权限 → 弹确认 → 真正安装
  const preflightInstall = async (source: string) => {
    if (!source.trim()) return;
    setConfirming({ source: source.trim(), sha256: "", permissions: [] });
  };

  const installFromMarket = async (item: any) => {
    setConfirming({ source: item.source_url || "", sha256: item.sha256 || "", permissions: [] });
  };

  // ── 多市场源 & 生态安装（Phase 1） ──
  const addSource = async () => {
    if (!newSourceUrl.trim()) { showToast(t("tools.paste_market_url"), "warn"); return; }
    try {
      const resp = await authFetch("/v1/marketplace/sources", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: newSourceUrl.trim() }),
      });
      const data = await resp.json();
      if (data.status === "ok") { showToast(data.message || t("tools.added")); setNewSourceUrl(""); setShowSourceForm(false); refreshSources(); refreshMarket(); }
      else { showToast(data.message || t("tools.add_fail"), "warn"); }
    } catch (e) { console.error(e); showToast(t("tools.add_fail"), "warn"); }
  };

  const removeSource = async (src: any) => {
    if (src.builtin && src.removed === false) { showToast(t("tools.builtin_undeletable"), "warn"); return; }
    try {
      const resp = await authFetch("/v1/marketplace/sources", {
        method: "DELETE", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: src.url }),
      });
      const data = await resp.json();
      if (data.status === "ok") { showToast(data.message || t("tools.removed")); refreshSources(); refreshMarket(); }
      else { showToast(data.message || t("tools.remove_fail"), "warn"); }
    } catch (e) { console.error(e); showToast(t("tools.remove_fail"), "warn"); }
  };

  // 生态条目安装：走 install-github（下载→打包→安装），复用确认流
  // 封锁/解封来源（最小治理：安装前由后端拦截）
  const toggleSourceBlocked = async (src: any) => {
    const key = (src.repo || src.url || "").toLowerCase();
    const isBlocked = blockedSources.some(b => b === key || key.endsWith("/" + b) || b === src.url?.toLowerCase());
    try {
      const resp = await authFetch("/v1/extensions/source-policy", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: src.url, blocked: !isBlocked }),
      });
      const data = await resp.json();
      if (data.status === "ok") { showToast(data.message || t("tools.updated")); refreshSources(); }
      else showToast(data.message || t("tools.op_fail"), "warn");
    } catch (e) { console.error(e); showToast(t("tools.op_fail"), "warn"); }
  };

  const installGitHubItem = async (item: any) => {
    setConfirming({
      source: t("tools.source_eco", { repo: item.repo || item.source_url }),
      sha256: item.sha256 || "",
      permissions: item.permissions || [],
      githubItem: item,
      isGitHubItem: true,
    });
  };

  const toggleExt = async (ext: ExtensionInfo) => {
    try {
      const resp = await authFetch("/v1/extensions/set-enabled", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: ext.name, enabled: !ext.enabled }),
      });
      const data = await resp.json();
      if (data.status !== "ok") { showToast(data.message || t("tools.op_fail"), "warn"); return; }
      refreshExtensions();
      refreshCapabilities();
    } catch (e) { console.error(e); showToast(t("tools.op_fail"), "warn"); }
  };

  const uninstallExt = async (ext: ExtensionInfo) => {
    try {
      const resp = await authFetch("/v1/extensions/uninstall", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: ext.name }),
      });
      const data = await resp.json();
      if (data.status !== "ok") { showToast(data.message || t("tools.uninstall_fail"), "warn"); return; }
      showToast(data.message || t("tools.uninstalled"));
      refreshExtensions();
      refreshCapabilities();
    } catch (e) { console.error(e); showToast(t("tools.uninstall_fail"), "warn"); }
  };

  // ── 统一能力操作 ──
  const toggleCap = async (cap: Capability) => {
    try {
      const resp = await authFetch(`/v1/capabilities/${cap.name}/toggle`, { method: "POST" });
      const data = await resp.json();
      if (data.status !== "ok") { showToast(data.message || t("tools.op_fail"), "warn"); return; }
      setCapabilities(prev => prev.map(c => c.name === cap.name ? { ...c, enabled: data.enabled } : c));
    } catch (e) { console.error(e); showToast(t("tools.op_fail"), "warn"); }
  };

  const togglePerm = async (cap: Capability) => {
    const newPerm = cap.permission === "confirm" ? "safe" : "confirm";
    try {
      const resp = await authFetch(`/v1/capabilities/${cap.name}/permission`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ permission: newPerm }),
      });
      const data = await resp.json();
      if (data.status !== "ok") { showToast(data.message || t("tools.op_fail"), "warn"); return; }
      setCapabilities(prev => prev.map(c => c.name === cap.name ? { ...c, permission: newPerm } : c));
      showToast(`${cap.name} → ${newPerm === "safe" ? t("perm.safe") : t("perm.confirm")}`);
    } catch (e) { console.error(e); showToast(t("tools.op_fail"), "warn"); }
  };

  const deleteCap = async (cap: Capability) => {
    try {
      const resp = await authFetch(`/v1/capabilities/skills/${cap.name}`, { method: "DELETE" });
      const data = await resp.json();
      if (data.status !== "ok") { showToast(data.message || t("tools.delete_fail"), "warn"); return; }
      setCapabilities(prev => prev.filter(c => c.name !== cap.name));
      showToast(t("tools.deleted"));
    } catch (e) { console.error(e); showToast(t("tools.delete_fail"), "warn"); }
  };

  const createSkill = async () => {
    if (!newSkillName.trim() || !newSkillContent.trim()) { showToast(t("tools.skill_need_name"), "warn"); return; }
    try {
      const resp = await authFetch("/v1/capabilities/skills", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newSkillName, content: newSkillContent }),
      });
      const data = await resp.json();
      if (data.status !== "ok") { showToast(data.message || t("tools.create_fail"), "warn"); return; }
      setCapabilities(prev => [...prev.filter(c => c.name !== data.skill.name), data.skill]);
      setNewSkillName(""); setNewSkillContent(""); setSkillFormOpen(false);
      showToast(t("tools.skill_created"));
    } catch (e) { console.error(e); showToast(t("tools.create_fail"), "warn"); }
  };

  const saveTavilyKey = async (key: string) => {
    if (!key.trim()) { showToast(t("skills.tavily_fill_key"), "warn"); return; }
    setTavilyKey(prev => ({ ...prev, loading: true }));
    try {
      const resp = await authFetch("/v1/settings/tavily-key", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      const data = await resp.json();
      if (data.status === "ok") {
        setTavilyKey({ hasKey: true, masked: data.masked, loading: false });
        showToast(t("skills.tavily_saved"));
      } else {
        setTavilyKey(prev => ({ ...prev, loading: false }));
        showToast(data.message || t("skills.save_fail"), "warn");
      }
    } catch (e) { console.error(e); setTavilyKey(prev => ({ ...prev, loading: false })); showToast(t("skills.save_fail"), "warn"); }
  };

  const deleteTavilyKey = async () => {
    setTavilyKey(prev => ({ ...prev, loading: true }));
    try {
      const resp = await authFetch("/v1/settings/tavily-key", { method: "DELETE" });
      const data = await resp.json();
      if (data.status === "ok") {
        setTavilyKey({ hasKey: false, masked: null, loading: false });
        showToast(t("skills.tavily_deleted"));
      } else {
        setTavilyKey(prev => ({ ...prev, loading: false }));
        showToast(data.message || t("skills.delete_fail"), "warn");
      }
    } catch (e) { console.error(e); setTavilyKey(prev => ({ ...prev, loading: false })); showToast(t("skills.delete_fail"), "warn"); }
  };

  const authorName = (a: ExtensionInfo["author"]) =>
    typeof a === "string" ? a : (a?.name || "");

  // 来源是唯一保留的区分信息：决定条目能否删除（只有自建可删）
  const sourceLabel = (src: string) =>
    src === "builtin" ? t("tools.source_builtin") : src === "user" ? t("tools.badge_custom") : src.replace(/^extension:/, t("tools.badge_ext"));

  // 市场搜索：按名称/描述/来源过滤（890 条纯前端，零压力）
  const q = marketQuery.trim().toLowerCase();
  const catOf = (p: any): string => {
    if (p.source_kind === "claude-plugin") return "claude";
    if (p.source_kind === "openclaw-skill" || p.source_kind === "generic-skill") {
      // 手动源 vs 发现源：靠 market_source 区分
      return p.market_source === "GitHub 发现" ? "discovered" : "openclaw";   // 后端字段值，不翻译
    }
    return "official";
  };
  const filteredPlugins = marketPlugins.filter((p) => {
    if (marketCat !== "all" && catOf(p) !== marketCat) return false;
    if (!q) return true;
    const hay = `${p.display_name || p.name || ""} ${p.description || ""} ${p.market_source || ""} ${p.repo || ""}`.toLowerCase();
    return hay.includes(q);
  });

  return (
    <div>
      {/* ═══ 扩展市场 ═══ */}
      <div className="card-desc" style={{ marginBottom: 12 }}>
        {t("tools.market_desc")}
      </div>

      {/* 市场 / 已装 tab */}
      <div className="tab-bar" style={{ marginBottom: 12 }}>
        <button className={`tab-btn${marketTab === "market" ? " active" : ""}`}
          onClick={() => setMarketTab("market")}>{t("tools.market_tab")}</button>
        <button className={`tab-btn${marketTab === "installed" ? " active" : ""}`}
          onClick={() => setMarketTab("installed")}>{t("tools.installed_tab")}</button>
      </div>

      {marketTab === "market" && (
      <>
        {/* 市场源管理 */}
        <div className="card" style={{ marginBottom: 14 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <div className="card-title" style={{ marginBottom: 0 }}>{t("tools.tab_sources")}</div>
            <button className="btn btn-sm btn-ghost" onClick={() => setShowSourceForm(!showSourceForm)}>
              {showSourceForm ? t("common.cancel") : t("tools.add_source")}
            </button>
          </div>
          {showSourceForm && (
            <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
              <input
                className="text-input"
                style={{ flex: 1 }}
                placeholder={t("tools.source_placeholder")}
                value={newSourceUrl}
                onChange={(e) => setNewSourceUrl(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") addSource(); }}
              />
              <button className="btn btn-sm btn-primary" onClick={addSource}>{t("common.add")}</button>
            </div>
          )}
          <div className="card-desc" style={{ marginTop: 8, fontSize: 11 }}>
            {t("tools.source_hint")}
          </div>
          <div style={{ marginTop: 6 }}>
            {marketSources.map((src) => (
              <div key={src.id || src.url} style={{
                display: "flex", alignItems: "center", gap: 8, padding: "5px 0",
                borderBottom: "1px solid var(--border-default)",
              }}>
                <span style={{ fontSize: 12, fontWeight: 600 }}>{src.removed ? t("tools.ignored") + " " : ""}{src.name}</span>
                <span className="badge badge-safe">{src.kind}</span>
                <span style={{ flex: 1, fontSize: 10, color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {src.description || src.url}
                </span>
                {(() => {
                  const key = (src.url || "").toLowerCase();
                  const isBlocked = blockedSources.some(b => b === key);
                  return (
                    <>
                      {src.builtin && <span style={{ fontSize: 10, color: "var(--text-muted)" }}>{t("tools.source_builtin")}</span>}
                      <button className="btn btn-xs"
                        style={{ color: isBlocked ? "var(--danger)" : "var(--text-muted)", padding: "1px 6px", fontSize: 10 }}
                        onClick={() => toggleSourceBlocked(src)}
                        title={isBlocked ? t("tools.unblock_source") : t("tools.block_source")}>
                        {isBlocked ? t("tools.blocked") : t("tools.block")}
                      </button>
                      {!src.builtin && (
                        <button className="btn-icon" style={{ fontSize: 12, color: "var(--danger)" }}
                          onClick={() => removeSource(src)} title={t("tools.remove_source")}>✕</button>
                      )}
                    </>
                  );
                })()}
              </div>
            ))}
          </div>
        </div>

        {/* GitHub 自动发现（Discovery Engine：主动抓取生态仓库） */}
        <div className="card" style={{ marginBottom: 14 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <div className="card-title" style={{ marginBottom: 0 }}>{t("tools.discover_title")}</div>
            <button className="btn btn-sm btn-ghost" disabled={discoverRefreshing}
              onClick={triggerDiscoverRefresh}>
              {discoverRefreshing ? t("tools.fetching") : t("tools.fetch_now")}
            </button>
          </div>
          <div className="card-desc" style={{ marginTop: 6, fontSize: 11 }}>
            {t("tools.discover_desc")}
            <b style={{ color: "var(--accent)" }}> {t("tools.discover_skills", { n: discoverState.entries || 0 })}</b>
            {t("tools.discover_from_repos", { n: discoverState.repos || 0 })}
            {discoverState.last_scan_ts
              ? t("tools.last_scan", { ts: new Date(discoverState.last_scan_ts * 1000).toLocaleString() })
              : t("tools.never_scanned")}
          </div>
        </div>

        {/* 聚合列表（按源分组，默认折叠） */}
        <div className="card" style={{ marginBottom: 14 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", cursor: "pointer" }}
            onClick={() => setMarketCollapsed(c => !c)}>
            <div className="card-title" style={{ marginBottom: 0 }}>
              {marketCollapsed ? "▸" : "▾"} {t("tools.market_collapsed_title")}
              <span className="badge badge-safe" style={{ marginLeft: 6, fontFamily: "var(--font-mono)" }}>
                {marketLoading ? "…" : marketPlugins.length}
              </span>
            </div>
            {!marketCollapsed && (
              <button className="btn btn-xs btn-ghost" onClick={(e) => { e.stopPropagation(); setMarketCollapsed(true); }}>{t("common.collapse")}</button>
            )}
          </div>
          {!marketCollapsed && (<>
          {marketLoading && <div className="card-desc">{t("common.loading")}</div>}
          {marketErr && <div className="card-desc" style={{ color: "var(--danger)" }}>{marketErr}</div>}
          {/* 搜索框 */}
          <div style={{ display: "flex", gap: 8, marginBottom: 8, marginTop: 10 }}>
            <input
              className="text-input"
              style={{ flex: 1, fontSize: 12 }}
              placeholder={t("tools.search_placeholder")}
              value={marketQuery}
              onChange={(e) => setMarketQuery(e.target.value)}
            />
            {marketQuery && (
              <button className="btn btn-xs btn-ghost" onClick={() => setMarketQuery("")}>{t("common.clear")}</button>
            )}
          </div>
          {/* 类别筛选 chips */}
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
            {([
              ["all", t("common.all")],
              ["official", t("tools.kind_official")],
              ["openclaw", "OpenClaw"],
              ["claude", t("tools.kind_claude")],
              ["discovered", t("tools.tab_discover")],
            ] as [string, string][]).map(([k, label]) => (
              <button key={k} className={`btn btn-xs ${marketCat === k ? "btn-primary" : "btn-ghost"}`}
                onClick={() => setMarketCat(k as any)}>{label}</button>
            ))}
          </div>
          <div className="card-desc" style={{ marginBottom: 6, fontSize: 11 }}>
            {t("tools.matched_of", { total: marketPlugins.length, n: filteredPlugins.length })}
          </div>
          {!marketLoading && !marketErr && marketPlugins.length === 0 && (
            <div className="card-desc" style={{ marginTop: 8 }}>{t("tools.market_empty")}</div>
          )}
          {filteredPlugins.length === 0 && marketPlugins.length > 0 && (
            <div className="card-desc" style={{ marginTop: 8 }}>{t("tools.no_match_query", { q: marketQuery })}</div>
          )}
          {filteredPlugins.map((item) => {
            const isEco = !!item.source_kind && item.source_kind !== "";
            const onClick = isEco
              ? () => installGitHubItem(item)
              : () => (item.installed && item.update_available
                  ? setConfirming({ source: item.source_url, sha256: item.sha256, permissions: [] })
                  : installFromMarket(item));
            return (
            <div key={`${item.repo || item.source_url || ""}:${item.skill_path || item.name}`} style={{
              display: "flex", alignItems: "center", gap: 10, padding: "8px 0",
              borderBottom: "1px solid var(--border-default)",
            }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ fontWeight: 650, fontSize: 13 }}>{isEco ? "🌐" : "🧩"} {item.display_name || item.name}</span>
                  <span className="badge badge-safe">v{item.version}</span>
                  {item.market_source && <span className="badge badge-active">{item.market_source}</span>}
                  {item.update_available && <span className="badge badge-confirm">{t("tools.has_update")}</span>}
                  {item.external_deps && <span className="badge badge-inactive" title={t("tools.ext_dep_hint")}>{t("tools.ext_dep_badge")}</span>}
                </div>
                <div className="card-desc" style={{ marginTop: 2 }}>{(item.description || "").slice(0, 140)}</div>
                <div className="card-meta" style={{ marginTop: 2 }}>
                  {typeof item.author === "object" && item.author ? item.author.name || "" : ""}
                  {item.category ? ` · ${item.category}` : ""}
                  {isEco ? ` · ${item.repo || ""}` : ""}
                  {typeof item.stars === "number" && item.stars > 0 ? ` · ⭐ ${item.stars.toLocaleString()}` : ""}
                </div>
              </div>
              <button className={`btn btn-sm ${item.installed && !item.update_available ? "btn-ghost" : "btn-primary"}`}
                disabled={item.installed && !item.update_available}
                onClick={onClick}>
                {item.update_available ? t("common.update") : item.installed ? t("tools.installed") : t("common.install")}
              </button>
            </div>
          )})}
          </>)}
        </div>
      </>
      )}

      {marketTab === "installed" && (
      <>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-title" style={{ marginBottom: 8 }}>{t("tools.install_ext")}</div>
        <div style={{ display: "flex", gap: 8 }}>
          <input
            className="text-input"
            style={{ flex: 1 }}
            placeholder={t("tools.install_placeholder")}
            value={installSrc}
            onChange={(e) => setInstallSrc(e.target.value)}
          />
          <button className="btn btn-primary" disabled={installing || !installSrc.trim()}
            onClick={() => preflightInstall(installSrc)}>
            {installing ? t("tools.installing") : t("common.install")}
          </button>
        </div>
        <div className="card-desc" style={{ marginTop: 6 }}>
          {t("tools.install_source_hint")}
        </div>
      </div>

      <div className="card-grid">
        {extensions.map((ext) => (
          <div key={ext.name} className="card" style={ext.enabled ? {} : { opacity: 0.55 }}>
            <div className="card-title">
              🧩 {ext.name}
              <span className="badge badge-safe">v{ext.version}</span>
              {!ext.enabled && <span className="badge badge-confirm">{t("tools.disabled")}</span>}
            </div>
            <div className="card-desc">{ext.description}</div>
            <div className="card-meta" style={{ marginTop: 6 }}>
              <span>{authorName(ext.author) || t("tools.unknown_author")}</span>
              <span> · </span>
              <span>{(ext.permissions || []).map((p) => (PERM_LABEL[p] ? t(PERM_LABEL[p]) : p)).join(" / ") || t("perm.readonly")}</span>
            </div>
            <div className="card-meta" style={{ marginTop: 4 }}>
              <span>📦 {ext.has_plugin ? t("tools.kind_plugin") : ""}{ext.has_skills ? " " + t("tools.kind_skill") : ""}{ext.has_agents ? " " + t("tools.kind_subagent") : ""}</span>
            </div>
            <div style={{ display: "flex", gap: 6, marginTop: 10 }}>
              <button className={`btn btn-sm ${ext.enabled ? "btn-ghost" : "btn-primary"}`}
                onClick={() => toggleExt(ext)}>{ext.enabled ? t("common.disable") : t("common.enable")}</button>
              <button className="btn btn-sm btn-ghost" onClick={() => uninstallExt(ext)}>{t("common.uninstall")}</button>
            </div>
          </div>
        ))}
        {extensions.length === 0 && (
          <div className="card" style={{ gridColumn: "1 / -1" }}>
            <div className="card-desc">
              {t("tools.no_ext_installed")}
            </div>
          </div>
        )}
      </div>
      </>
      )}

      {/* 权限确认弹层（模态居中——此前内嵌卡片排在长列表末尾、视口外，表现为"点了没反应"） */}
      {confirming && (
        <div style={{
          position: "fixed", inset: 0, zIndex: 1000,
          background: "rgba(0,0,0,0.55)",
          display: "flex", alignItems: "center", justifyContent: "center",
        }} onClick={() => { if (!installing) setConfirming(null); }}>
          <div className="card" style={{
            width: "min(560px, 92vw)", maxHeight: "80vh", overflowY: "auto",
            borderLeft: "2px solid var(--warning)", background: "var(--bg-card)",
            boxShadow: "0 20px 60px rgba(0,0,0,0.5)",
          }} onClick={(e) => e.stopPropagation()}>
            <div className="card-title">{t("tools.confirm_source")}</div>
            <div className="card-desc" style={{ marginTop: 4, wordBreak: "break-all" }}>
              {confirming.source}
            </div>
            <div className="card-desc" style={{ marginTop: 4 }}>
              {confirming.isGitHubItem
                ? t("tools.confirm_perms", { perms: (confirming.permissions || []).join("/") || t("perm.readonly") })
                : t("tools.confirm_manifest")}
            </div>
            {confirming.isGitHubItem && confirming.githubItem?.external_deps && (
              <div className="card-desc" style={{ marginTop: 4, color: "var(--warning)" }}>
                {t("tools.ext_dep_modal")}
              </div>
            )}
            <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
              <button className="btn btn-primary" disabled={installing}
                onClick={() => doInstall(confirming.source, confirming.sha256)}>{installing ? t("tools.installing_download") : t("tools.confirm_install")}</button>
              <button className="btn btn-ghost" onClick={() => setConfirming(null)}>{t("common.cancel")}</button>
            </div>
          </div>
        </div>
      )}

      {/* ═══ 统一能力列表（工具与技能，一个列表不分栏） ═══ */}
      <div className="page-header" style={{ margin: "14px 0 8px" }}>
        <div className="card-title" style={{ fontSize: 14 }}>{t("tools.capability")}</div>
        <div className="card-desc" style={{ marginTop: 2, fontSize: 11 }}>
          {t("tools.caps_desc")}
        </div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        {capabilities.map((cap) => {
          const isTool = cap.kind === "tool";
          const permBadge = cap.permission === "safe" ? "badge-safe"
            : cap.permission === "confirm" ? "badge-confirm" : "badge-inactive";
          return (
          <div key={`${cap.kind}:${cap.name}`} style={{
            display: "flex", alignItems: "center", gap: 8, padding: "4px 8px",
            borderRadius: "var(--radius-sm)",
            background: cap.enabled ? "transparent" : "color-mix(in srgb, var(--bg-card) 55%, transparent)",
            opacity: cap.enabled ? 1 : 0.55,
          }}>
            <span style={{ fontSize: 13, flexShrink: 0 }}>⚙️</span>
            <span style={{ fontWeight: 600, fontSize: 12, flexShrink: 0, minWidth: 110, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {cap.display_name || cap.name}
            </span>
            <span className={`badge ${permBadge}`} style={{ flexShrink: 0 }}>
              {CAP_PERM_LABEL[cap.permission] ? t(CAP_PERM_LABEL[cap.permission]) : cap.permission}
            </span>
            {!cap.enabled && <span className="badge badge-confirm" style={{ flexShrink: 0 }}>{t("tools.disabled")}</span>}
            <span style={{ flex: 1, minWidth: 0, fontSize: 11, color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {(() => {
                // 描述来自后端（同时是给模型的工具说明，不能动）→ 显示层优先用 i18n 摘要；
                // t() 找不到键时会原样返回键名，正好用来判断"有没有本地化版本"。
                const k = `cap.desc.${cap.name}`;
                const localized = t(k);
                return localized !== k ? localized : cap.description;
              })()}
            </span>
            <span style={{ flexShrink: 0, fontSize: 10, color: "var(--text-muted)" }}>
              {t("tools.calls", { count: cap.usage_count })} · {sourceLabel(cap.source)}
            </span>
            <span style={{ display: "flex", gap: 4, flexShrink: 0, alignItems: "center" }}>
              <button className="btn btn-xs btn-ghost"
                onClick={() => toggleCap(cap)}>{cap.enabled ? t("common.disable") : t("common.enable")}</button>
              {isTool && (
                <button className="btn btn-xs btn-ghost" onClick={() => togglePerm(cap)}>
                  {cap.permission === "safe" ? t("tools.set_confirm") : t("tools.set_safe")}
                </button>
              )}
              {!isTool && cap.source === "user" && (
                <button className="btn btn-xs btn-ghost" style={{ color: "var(--danger)" }}
                  onClick={() => deleteCap(cap)}>{t("common.delete")}</button>
              )}
            </span>
          </div>
        )})}
        {capabilities.length === 0 && (
          <div className="card-desc">{t("tools.no_capability")}</div>
        )}
      </div>

      {/* ── 新建技能 ── */}
      <div style={{ marginTop: 16, padding: 14, background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-lg)" }}>
        {!skillFormOpen ? (
          <button className="btn btn-sm btn-ghost" onClick={() => setSkillFormOpen(true)}>＋ {t("skills.new")}</button>
        ) : (
          <>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>{t("skills.new")}</div>
            <input className="form-input" style={{ fontSize: 11, marginBottom: 6 }} placeholder={t("skills.name_placeholder")}
              value={newSkillName} onChange={e => setNewSkillName(e.target.value)} />
            <textarea className="form-input" style={{ fontSize: 11, minHeight: 80, resize: "vertical", fontFamily: "var(--font-mono)" }}
              placeholder={t("skills.content_placeholder")}
              value={newSkillContent} onChange={e => setNewSkillContent(e.target.value)} />
            <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
              <button className="btn btn-sm btn-primary" onClick={createSkill}>{t("skills.create")}</button>
              <button className="btn btn-sm btn-ghost" onClick={() => setSkillFormOpen(false)}>{t("skills.tavily_cancel")}</button>
            </div>
          </>
        )}
      </div>

      {/* ── Tavily API Key 配置（tavily_search 能力存在时） ── */}
      {capabilities.some(c => c.name === "tavily_search") && (
        <div style={{ marginTop: 16, padding: 12, background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-lg)" }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>{t("skills.tavily_title")}</div>
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 8 }}>
            {t("skills.tavily_desc")}
          </div>
          {!showKeyInput && !tavilyKey.hasKey && (
            <button className="btn btn-sm btn-primary" onClick={() => setShowKeyInput(true)}>
              {t("skills.tavily_configure")}
            </button>
          )}
          {!showKeyInput && tavilyKey.hasKey && (
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--success)" }}>
                🔑 {tavilyKey.masked}
              </span>
              <button className="btn btn-sm btn-ghost" onClick={() => { setKeyInput(""); setShowKeyInput(true); }}>
                {t("skills.tavily_modify")}
              </button>
              <button className="btn btn-sm btn-ghost" style={{ color: "var(--danger)" }}
                onClick={deleteTavilyKey} disabled={tavilyKey.loading}>
                {t("skills.tavily_delete")}
              </button>
            </div>
          )}
          {showKeyInput && (
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input className="form-input" style={{ flex: 1, margin: 0, fontSize: 11, padding: "6px 10px", fontFamily: "var(--font-mono)" }}
                type="password" placeholder="tvly-..." value={keyInput}
                onChange={e => setKeyInput(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter") { saveTavilyKey(keyInput); setShowKeyInput(false); } }}
                autoFocus />
              <button className="btn btn-sm btn-primary"
                onClick={() => { saveTavilyKey(keyInput); setShowKeyInput(false); }}
                disabled={tavilyKey.loading || !keyInput.trim()}>
                {tavilyKey.loading ? "..." : t("skills.tavily_save")}
              </button>
              <button className="btn btn-sm btn-ghost" onClick={() => setShowKeyInput(false)}>{t("skills.tavily_cancel")}</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
