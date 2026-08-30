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

const PERM_LABEL: Record<string, string> = {
  readonly: "只读",
  files: "文件读写",
  network: "网络",
  shell: "命令执行",
};

const CAP_PERM_LABEL: Record<string, string> = {
  safe: "安全",
  confirm: "需确认",
  danger: "高危",
  deny: "禁止",
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
  const [newSourceUrl, setNewSourceUrl] = useState("");
  const [showSourceForm, setShowSourceForm] = useState(false);
  // ── GitHub 自动发现（Discovery Engine） ──
  const [discoverState, setDiscoverState] = useState<any>({ last_scan_ts: 0, repos: 0, entries: 0 });
  const [discoverRefreshing, setDiscoverRefreshing] = useState(false);
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
        setMarketErr(data.message || "市场加载失败");
      } catch (e) {
        console.error("市场加载失败(尝试" + (attempt + 1) + "):", e);
        if (attempt === 0) { await new Promise(r => setTimeout(r, 800)); continue; }
        setMarketErr("市场加载失败（" + String((e as Error)?.message || e) + "）");
      }
    }
    setMarketLoading(false);
  }, []);

  const refreshSources = useCallback(async () => {
    try {
      const resp = await authFetch("/v1/marketplace/sources");
      const data = await resp.json();
      if (data.status === "ok") setMarketSources(data.sources || []);
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
        showToast(data.message || "GitHub 抓取已开始");
        // 轮询状态直到完成（抓取 3-5 分钟）
        setTimeout(async () => { await refreshDiscover(); await refreshMarket(); setDiscoverRefreshing(false); }, 12000);
      } else { showToast(data.message || "刷新失败", "warn"); setDiscoverRefreshing(false); }
    } catch (e) { console.error(e); showToast("刷新失败", "warn"); setDiscoverRefreshing(false); }
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
        showToast(data.message || "已安装");
        setInstallSrc("");
        refreshExtensions();
        refreshCapabilities();
      } else {
        showToast(data.message || "安装失败", "warn");
      }
    } catch (e) { console.error(e); showToast("安装请求失败", "warn"); }
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
    if (!newSourceUrl.trim()) { showToast("请粘贴市场地址", "warn"); return; }
    try {
      const resp = await authFetch("/v1/marketplace/sources", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: newSourceUrl.trim() }),
      });
      const data = await resp.json();
      if (data.status === "ok") { showToast(data.message || "已添加"); setNewSourceUrl(""); setShowSourceForm(false); refreshSources(); refreshMarket(); }
      else { showToast(data.message || "添加失败", "warn"); }
    } catch (e) { console.error(e); showToast("添加失败", "warn"); }
  };

  const removeSource = async (src: any) => {
    if (src.builtin && src.removed === false) { showToast("内置源不可删除", "warn"); return; }
    try {
      const resp = await authFetch("/v1/marketplace/sources", {
        method: "DELETE", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: src.url }),
      });
      const data = await resp.json();
      if (data.status === "ok") { showToast(data.message || "已移除"); refreshSources(); refreshMarket(); }
      else { showToast(data.message || "移除失败", "warn"); }
    } catch (e) { console.error(e); showToast("移除失败", "warn"); }
  };

  // 生态条目安装：走 install-github（下载→打包→安装），复用确认流
  const installGitHubItem = async (item: any) => {
    setConfirming({
      source: `生态源: ${item.repo || item.source_url}`,
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
      if (data.status !== "ok") { showToast(data.message || "操作失败", "warn"); return; }
      refreshExtensions();
      refreshCapabilities();
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
      refreshCapabilities();
    } catch (e) { console.error(e); showToast("卸载失败", "warn"); }
  };

  // ── 统一能力操作 ──
  const toggleCap = async (cap: Capability) => {
    try {
      const resp = await authFetch(`/v1/capabilities/${cap.name}/toggle`, { method: "POST" });
      const data = await resp.json();
      if (data.status !== "ok") { showToast(data.message || "操作失败", "warn"); return; }
      setCapabilities(prev => prev.map(c => c.name === cap.name ? { ...c, enabled: data.enabled } : c));
    } catch (e) { console.error(e); showToast("操作失败", "warn"); }
  };

  const togglePerm = async (cap: Capability) => {
    const newPerm = cap.permission === "confirm" ? "safe" : "confirm";
    try {
      const resp = await authFetch(`/v1/capabilities/${cap.name}/permission`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ permission: newPerm }),
      });
      const data = await resp.json();
      if (data.status !== "ok") { showToast(data.message || "操作失败", "warn"); return; }
      setCapabilities(prev => prev.map(c => c.name === cap.name ? { ...c, permission: newPerm } : c));
      showToast(`${cap.name} → ${newPerm === "safe" ? "安全" : "需确认"}`);
    } catch (e) { console.error(e); showToast("操作失败", "warn"); }
  };

  const deleteCap = async (cap: Capability) => {
    try {
      const resp = await authFetch(`/v1/capabilities/skills/${cap.name}`, { method: "DELETE" });
      const data = await resp.json();
      if (data.status !== "ok") { showToast(data.message || "删除失败", "warn"); return; }
      setCapabilities(prev => prev.filter(c => c.name !== cap.name));
      showToast("已删除");
    } catch (e) { console.error(e); showToast("删除失败", "warn"); }
  };

  const createSkill = async () => {
    if (!newSkillName.trim() || !newSkillContent.trim()) { showToast("请填写技能名称和内容", "warn"); return; }
    try {
      const resp = await authFetch("/v1/capabilities/skills", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newSkillName, content: newSkillContent }),
      });
      const data = await resp.json();
      if (data.status !== "ok") { showToast(data.message || "创建失败", "warn"); return; }
      setCapabilities(prev => [...prev.filter(c => c.name !== data.skill.name), data.skill]);
      setNewSkillName(""); setNewSkillContent(""); setSkillFormOpen(false);
      showToast("技能已创建");
    } catch (e) { console.error(e); showToast("创建失败", "warn"); }
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
    src === "builtin" ? "内置" : src === "user" ? "自建" : src.replace(/^extension:/, "扩展·");

  return (
    <div>
      {/* ═══ 扩展市场 ═══ */}
      <div className="card-desc" style={{ marginBottom: 12 }}>
        无缝安装：拖入 .latiaoext 文件、粘贴 URL 或 GitHub 仓库地址即可安装工具插件、技能与子智能体组合包
      </div>

      {/* 市场 / 已装 tab */}
      <div className="tab-bar" style={{ marginBottom: 12 }}>
        <button className={`tab-btn${marketTab === "market" ? " active" : ""}`}
          onClick={() => setMarketTab("market")}>市场</button>
        <button className={`tab-btn${marketTab === "installed" ? " active" : ""}`}
          onClick={() => setMarketTab("installed")}>已安装</button>
      </div>

      {marketTab === "market" && (
      <>
        {/* 市场源管理 */}
        <div className="card" style={{ marginBottom: 14 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <div className="card-title" style={{ marginBottom: 0 }}>市场源</div>
            <button className="btn btn-sm btn-ghost" onClick={() => setShowSourceForm(!showSourceForm)}>
              {showSourceForm ? "取消" : "＋ 添加源"}
            </button>
          </div>
          {showSourceForm && (
            <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
              <input
                className="text-input"
                style={{ flex: 1 }}
                placeholder="marketplace.json URL / GitHub 仓库（自动识别格式）"
                value={newSourceUrl}
                onChange={(e) => setNewSourceUrl(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") addSource(); }}
              />
              <button className="btn btn-sm btn-primary" onClick={addSource}>添加</button>
            </div>
          )}
          <div className="card-desc" style={{ marginTop: 8, fontSize: 11 }}>
            官方市场是内置源；可添加任意 GitHub agent 仓库（OpenClaw 技能 / Claude Code 插件自动发现）或 marketplace.json 地址。
          </div>
          <div style={{ marginTop: 6 }}>
            {marketSources.map((src) => (
              <div key={src.id || src.url} style={{
                display: "flex", alignItems: "center", gap: 8, padding: "5px 0",
                borderBottom: "1px solid var(--border-default)",
              }}>
                <span style={{ fontSize: 12, fontWeight: 600 }}>{src.removed ? "(已忽略) " : ""}{src.name}</span>
                <span className="badge badge-safe">{src.kind}</span>
                <span style={{ flex: 1, fontSize: 10, color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {src.description || src.url}
                </span>
                {src.builtin ? (
                  <span style={{ fontSize: 10, color: "var(--text-muted)" }}>内置</span>
                ) : (
                  <button className="btn-icon" style={{ fontSize: 12, color: "var(--danger)" }}
                    onClick={() => removeSource(src)} title="移除源">✕</button>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* GitHub 自动发现（Discovery Engine：主动抓取生态仓库） */}
        <div className="card" style={{ marginBottom: 14 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <div className="card-title" style={{ marginBottom: 0 }}>🌐 GitHub 自动发现</div>
            <button className="btn btn-sm btn-ghost" disabled={discoverRefreshing}
              onClick={triggerDiscoverRefresh}>
              {discoverRefreshing ? "抓取中…" : "立即抓取"}
            </button>
          </div>
          <div className="card-desc" style={{ marginTop: 6, fontSize: 11 }}>
            系统从 GitHub 搜索 agent 技能仓库并自动解析 SKILL.md。当前已发现：
            <b style={{ color: "var(--accent)" }}> {discoverState.entries || 0} 个技能</b>
            · 来自 <b>{discoverState.repos || 0}</b> 个仓库
            {discoverState.last_scan_ts
              ? ` · 上次扫描 ${new Date(discoverState.last_scan_ts * 1000).toLocaleString("zh-CN")}`
              : " · 尚未扫描（点击立即抓取或等待启动扫描）"}
          </div>
        </div>

        {/* 聚合列表（按源分组） */}
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="card-title" style={{ marginBottom: 8 }}>扩展市场</div>
          {marketLoading && <div className="card-desc">加载中…</div>}
          {marketErr && <div className="card-desc" style={{ color: "var(--danger)" }}>{marketErr}</div>}
          {!marketLoading && !marketErr && marketPlugins.length === 0 && (
            <div className="card-desc">市场为空——等待官方扩展上架或添加 GitHub 源。已支持：粘贴 URL/GitHub 仓库/本地文件安装。</div>
          )}
          {marketPlugins.map((item) => {
            const isEco = !!item.source_kind && item.source_kind !== "";
            const onClick = isEco
              ? () => installGitHubItem(item)
              : () => (item.installed && item.update_available
                  ? setConfirming({ source: item.source_url, sha256: item.sha256, permissions: [] })
                  : installFromMarket(item));
            return (
            <div key={`${item.market_source}:${item.name}`} style={{
              display: "flex", alignItems: "center", gap: 10, padding: "8px 0",
              borderBottom: "1px solid var(--border-default)",
            }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ fontWeight: 650, fontSize: 13 }}>{isEco ? "🌐" : "🧩"} {item.display_name || item.name}</span>
                  <span className="badge badge-safe">v{item.version}</span>
                  {item.market_source && <span className="badge badge-active">{item.market_source}</span>}
                  {item.update_available && <span className="badge badge-confirm">有更新</span>}
                </div>
                <div className="card-desc" style={{ marginTop: 2 }}>{(item.description || "").slice(0, 140)}</div>
                <div className="card-meta" style={{ marginTop: 2 }}>
                  {typeof item.author === "object" && item.author ? item.author.name || "" : ""}
                  {item.category ? ` · ${item.category}` : ""}
                  {isEco ? ` · ${item.repo || ""}` : ""}
                </div>
              </div>
              <button className={`btn btn-sm ${item.installed && !item.update_available ? "btn-ghost" : "btn-primary"}`}
                disabled={item.installed && !item.update_available}
                onClick={onClick}>
                {item.update_available ? "更新" : item.installed ? "已安装" : "安装"}
              </button>
            </div>
          )})}
        </div>
      </>
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
            {confirming.isGitHubItem
              ? `社区内容将打包为扩展安装（权限：${(confirming.permissions || []).join("/") || "只读"}）。安装前请确认来源可信。`
              : "扩展包将获得其 manifest 声明的权限（只读/文件/网络/命令）。安装前请确认来源可信。"}
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

      {/* ═══ 统一能力列表（工具与技能，一个列表不分栏） ═══ */}
      <div className="page-header" style={{ margin: "14px 0 8px" }}>
        <div className="card-title" style={{ fontSize: 14 }}>⚙️ 能力</div>
        <div className="card-desc" style={{ marginTop: 2, fontSize: 11 }}>
          工具与技能统一管理：启用开关、权限级别、使用次数共用一套能力表；技能由模型按需调用（use_skill）
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
              {CAP_PERM_LABEL[cap.permission] || cap.permission}
            </span>
            {!cap.enabled && <span className="badge badge-confirm" style={{ flexShrink: 0 }}>已禁用</span>}
            <span style={{ flex: 1, minWidth: 0, fontSize: 11, color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {cap.description}
            </span>
            <span style={{ flexShrink: 0, fontSize: 10, color: "var(--text-muted)" }}>
              {t("tools.calls", { count: cap.usage_count })} · {sourceLabel(cap.source)}
            </span>
            <span style={{ display: "flex", gap: 4, flexShrink: 0, alignItems: "center" }}>
              <button className="btn btn-xs btn-ghost"
                onClick={() => toggleCap(cap)}>{cap.enabled ? "禁用" : "启用"}</button>
              {isTool && (
                <button className="btn btn-xs btn-ghost" onClick={() => togglePerm(cap)}>
                  {cap.permission === "safe" ? t("tools.set_confirm") : t("tools.set_safe")}
                </button>
              )}
              {!isTool && cap.source === "user" && (
                <button className="btn btn-xs btn-ghost" style={{ color: "var(--danger)" }}
                  onClick={() => deleteCap(cap)}>删除</button>
              )}
            </span>
          </div>
        )})}
        {capabilities.length === 0 && (
          <div className="card-desc">没有匹配的能力条目。</div>
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
