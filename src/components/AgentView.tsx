import { useState, useEffect } from "react";
import { sidecarFetch } from "../utils/api";
import { useTranslation } from "../i18n";

interface AgentInfo {
  id: string;
  name: string;
  display: string;
  role: string;
  tools: string[] | "all";
  custom: boolean;
}

interface AgentViewProps {
  activeAgent: string;
  setActiveAgent: (id: string) => void;
  showToast: (msg: string) => void;
}

const BUILTIN_IDENTITY: Record<string, string[]> = {
  latiao: ["IDENTITY", "SOUL", "AGENTS", "USER"],
  "code-reviewer": ["IDENTITY", "", "", ""],
  "doc-generator": ["IDENTITY", "", "", ""],
  debugger: ["IDENTITY", "SOUL", "AGENTS", ""],
  translator: ["", "", "", "USER"],
};

const TOOL_OPTIONS = [
  "read_file", "write_file", "list_dir", "run_cmd",
  "open_folder", "open_app", "search_files",
];

const DEFAULT_AGENTS: AgentInfo[] = [
  { id: "latiao", name: "agent.latiao", display: "agent.latiao_display", role: "orchestrator", tools: "all", custom: false },
  { id: "code-reviewer", name: "agent.code_reviewer", display: "agent.code_reviewer_display", role: "specialist", tools: ["read_file", "list_dir", "search_files"], custom: false },
  { id: "doc-generator", name: "agent.doc_generator", display: "agent.doc_generator_display", role: "specialist", tools: ["read_file", "list_dir", "search_files", "write_file"], custom: false },
  { id: "debugger", name: "agent.debugger", display: "agent.debugger_display", role: "specialist", tools: "all", custom: false },
  { id: "translator", name: "agent.translator", display: "agent.translator_display", role: "specialist", tools: ["read_file", "list_dir", "search_files", "write_file"], custom: false },
];

// Map sidecar agent id to i18n keys so names/display translate with the UI language
const BUILTIN_AGENT_I18N: Record<string, { name: string; display: string }> = {
  latiao: { name: "agent.latiao", display: "agent.latiao_display" },
  "code-reviewer": { name: "agent.code_reviewer", display: "agent.code_reviewer_display" },
  "doc-generator": { name: "agent.doc_generator", display: "agent.doc_generator_display" },
  debugger: { name: "agent.debugger", display: "agent.debugger_display" },
  translator: { name: "agent.translator", display: "agent.translator_display" },
};

export default function AgentView({ activeAgent, setActiveAgent, showToast }: AgentViewProps) {
  const { t } = useTranslation();
  const [agents, setAgents] = useState<AgentInfo[]>(DEFAULT_AGENTS);
  const [showForm, setShowForm] = useState(false);
  const [pendingSwitch, setPendingSwitch] = useState<string | null>(null);
  const [newAgent, setNewAgent] = useState({ id: "", name: "", identity: "", tools: ["read_file", "list_dir", "search_files"] as string[] });

  const fetchAgents = async () => {
    try {
      const data = await sidecarFetch("/v1/agents");
      if (data.status === "ok") {
        const agents = (data.agents as AgentInfo[]).map((a: AgentInfo) => {
          const i18n = BUILTIN_AGENT_I18N[a.id];
          if (i18n && !a.custom) {
            return { ...a, name: i18n.name, display: i18n.display };
          }
          return a;
        });
        setAgents(agents);
      }
    } catch { /* sidecar not running */ }
  };

  useEffect(() => { fetchAgents(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const toggleTool = (tool: string) => {
    setNewAgent(prev => ({
      ...prev,
      tools: prev.tools.includes(tool)
        ? prev.tools.filter(t => t !== tool)
        : [...prev.tools, tool],
    }));
  };

  const handleSave = async () => {
    if (!newAgent.id || !newAgent.name) {
      showToast(t("agent.fill_id_name"));
      return;
    }
    try {
      const data = await sidecarFetch("/v1/agents/save", "POST", {
        id: newAgent.id,
        name: newAgent.name,
        display: newAgent.name,
        identity: newAgent.identity || `You are ${newAgent.name}, a specialist agent.`,
        tools: newAgent.tools,
      });
      if (data.status === "ok") {
        showToast(t("agent.created", { name: newAgent.name }));
        setShowForm(false);
        setNewAgent({ id: "", name: "", identity: "", tools: ["read_file", "list_dir", "search_files"] });
        fetchAgents();
      } else {
        showToast(t("agent.create_fail", { msg: data.message || "" }));
      }
    } catch (e) { console.error("Agent operation failed:", e); showToast(t("agent.conn_fail")); }
  };

  const handleOpenFile = async (id: string, section?: string) => {
    try {
      const url = "/v1/identity/open/" + id + (section ? "?section=" + section : "");
      const data = await sidecarFetch(url, "POST");
      if (data.status !== "ok") showToast(data.message || "打开失败");
    } catch { showToast(t("agent.conn_fail")); }
  };

  const handleDelete = async (id: string) => {
    try {
      const data = await sidecarFetch("/v1/agents/" + id, "DELETE");
      if (data.status === "ok") {
        showToast(t("agent.deleted"));
        if (activeAgent === id) setActiveAgent("latiao");
        fetchAgents();
      }
    } catch (e) { console.error("Agent operation failed:", e); showToast(t("agent.conn_fail")); }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, maxWidth: 720 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {t("agent.count", { count: agents.length })}
        </span>
        <button className="btn btn-sm btn-primary" onClick={() => setShowForm(!showForm)}>
          {showForm ? t("agent.cancel") : t("agent.new_btn")}
        </button>
      </div>

      {showForm && (
        <div className="settings-group" style={{ marginBottom: 12 }}>
          <div className="settings-group-header">{t("agent.new_title")}</div>
          <div style={{ padding: "12px 16px", display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ display: "flex", gap: 8 }}>
              <input className="form-input" style={{ flex: 1, margin: 0, fontSize: 11, padding: "6px 10px" }}
                placeholder={t("agent.id_placeholder")} value={newAgent.id}
                onChange={e => setNewAgent({ ...newAgent, id: e.target.value })} />
              <input className="form-input" style={{ flex: 1, margin: 0, fontSize: 11, padding: "6px 10px" }}
                placeholder={t("agent.name_placeholder")} value={newAgent.name}
                onChange={e => setNewAgent({ ...newAgent, name: e.target.value })} />
            </div>
            <textarea className="form-input" style={{ margin: 0, fontSize: 11, padding: "6px 10px", minHeight: 60, resize: "vertical" }}
              placeholder={t("agent.identity_placeholder")} value={newAgent.identity}
              onChange={e => setNewAgent({ ...newAgent, identity: e.target.value })} />
            <div>
              <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 4 }}>{t("agent.tools_label")}</div>
              <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                {TOOL_OPTIONS.map(t => (
                  <label key={t} style={{ fontSize: 10, display: "flex", alignItems: "center", gap: 3, cursor: "pointer" }}>
                    <input type="checkbox" checked={newAgent.tools.includes(t)} onChange={() => toggleTool(t)} />
                    {t}
                  </label>
                ))}
              </div>
            </div>
            <button className="btn btn-sm btn-primary" onClick={handleSave}>{t("agent.save")}</button>
          </div>
        </div>
      )}

      {agents.map((a) => (
        <div key={a.id} className={`agent-row${activeAgent === a.id ? " current" : ""}`} onClick={() => handleOpenFile(a.id)} style={{cursor:"pointer"}}>
          <span style={{ fontSize: 24, flexShrink: 0 }}>{a.custom ? "🧩" : "✨"}</span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 13, fontWeight: 600, display: "flex", alignItems: "center", gap: 8 }}>
              {a.name.startsWith("agent.") ? t(a.name) : a.name} {activeAgent === a.id && <span className="badge badge-active">{t("agent.current")}</span>}
              {a.custom && <span className="badge badge-inactive">{t("agent.custom")}</span>}
            </div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>{a.display.includes("_display") ? t(a.display) : a.display}</div>
            <div style={{ display: "flex", gap: 4, marginTop: 6 }}>
              {(BUILTIN_IDENTITY[a.id] || ["CUSTOM"]).map((label, j) => label ? (
                <span key={j} className="identity-chip active" onClick={(e) => { e.stopPropagation(); handleOpenFile(a.id, label); }}>
                  <span className="chip-dot">●</span> {label}
                </span>
              ) : null)}
            </div>
          </div>
          <div style={{ textAlign: "right", flexShrink: 0, display: "flex", flexDirection: "column", gap: 4, width: 90 }}>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              {Array.isArray(a.tools) ? t("agent.tools_count", { count: a.tools.length }) : t("agent.tools_all")}
            </span>
            <button className="btn btn-sm btn-ghost" style={{ marginTop: 2 }}
              onClick={() => {
                if (a.id === activeAgent) return;
                if (pendingSwitch === a.id) {
                  setActiveAgent(a.id);
                  setPendingSwitch(null);
                  showToast(t("agent.switched", { name: a.name.startsWith("agent.") ? t(a.name) : a.name }));
                } else {
                  setPendingSwitch(a.id);
                }
              }}>
              {activeAgent === a.id ? t("agent.current_btn") : pendingSwitch === a.id ? t("agent.confirm_switch") : t("agent.switch")}
            </button>
            {a.custom && (
              <button className="btn btn-sm btn-ghost" style={{ color: "var(--danger)", fontSize: 10 }}
                onClick={() => handleDelete(a.id)}>{t("agent.delete")}</button>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
