import { useState } from "react";
import { useTranslation } from "../i18n";

interface SkillsViewProps {
  skills: { name: string; file: string; key: string; enabled: boolean; builtin?: boolean }[];
  newSkill: { name: string; content: string };
  setNewSkill: (s: { name: string; content: string }) => void;
  toggleSkill: (key: string) => void;
  deleteSkill: (key: string) => void;
  addSkill: () => void;
  tavilyKey: { hasKey: boolean; masked: string | null; loading: boolean };
  onSaveTavilyKey: (key: string) => void;
  onDeleteTavilyKey: () => void;
}

export default function SkillsView({
  skills, newSkill, setNewSkill, toggleSkill, deleteSkill, addSkill,
  tavilyKey, onSaveTavilyKey, onDeleteTavilyKey,
}: SkillsViewProps) {
  const { t } = useTranslation();
  const [keyInput, setKeyInput] = useState("");
  const [showInput, setShowInput] = useState(false);

  // Only show Tavily config if the plugin is loaded
  const hasTavily = skills.some(s => s.key === "tavily_search");

  return (
    <>
      <div className="card-grid">
        {skills.map((sk) => (
          <div key={sk.key} className="card" onClick={() => toggleSkill(sk.key)}
            style={sk.enabled ? {} : { opacity: 0.5 }}>
            <div className="card-title">
              <span style={{ fontSize: 18 }}>{sk.enabled ? "📗" : "📕"}</span> {sk.name}
              <span className={`badge ${sk.enabled ? "badge-active" : "badge-inactive"}`} style={{ marginLeft: 4 }}>
                {sk.enabled ? t("skills.enabled") : t("skills.disabled")}
              </span>
            </div>
            <div className="card-desc">{sk.file}</div>
            <button className="btn btn-sm btn-ghost" style={{ marginTop: 10, width: "100%" }}
              onClick={(e) => { e.stopPropagation(); toggleSkill(sk.key); }}>
              {sk.enabled ? t("skills.disable") : t("skills.enable")}
            </button>
            {!sk.builtin && (
              <button className="btn-icon" style={{ position: "absolute", top: 8, right: 8, fontSize: 12, color: "var(--text-muted)" }}
                onClick={(e) => { e.stopPropagation(); deleteSkill(sk.key); }} title={t("skills.delete")}>✕</button>
            )}
          </div>
        ))}
      </div>

      {/* Tavily API Key configuration section */}
      {hasTavily && (
        <div style={{ marginTop: 16, padding: 12, background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-lg)" }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>{t("skills.tavily_title")}</div>
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 8 }}>
            {t("skills.tavily_desc")}
          </div>
          {!showInput && !tavilyKey.hasKey && (
            <button className="btn btn-sm btn-primary" onClick={() => setShowInput(true)}>
              {t("skills.tavily_configure")}
            </button>
          )}
          {!showInput && tavilyKey.hasKey && (
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--success)" }}>
                🔑 {tavilyKey.masked}
              </span>
              <button className="btn btn-sm btn-ghost" onClick={() => { setKeyInput(""); setShowInput(true); }}>
                {t("skills.tavily_modify")}
              </button>
              <button className="btn btn-sm btn-ghost" style={{ color: "var(--danger)" }}
                onClick={onDeleteTavilyKey} disabled={tavilyKey.loading}>
                {t("skills.tavily_delete")}
              </button>
            </div>
          )}
          {showInput && (
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input className="form-input" style={{ flex: 1, margin: 0, fontSize: 11, padding: "6px 10px", fontFamily: "var(--font-mono)" }}
                type="password" placeholder="tvly-..." value={keyInput}
                onChange={e => setKeyInput(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter") { onSaveTavilyKey(keyInput); setShowInput(false); } }}
                autoFocus />
              <button className="btn btn-sm btn-primary"
                onClick={() => { onSaveTavilyKey(keyInput); setShowInput(false); }}
                disabled={tavilyKey.loading || !keyInput.trim()}>
                {tavilyKey.loading ? "..." : t("skills.tavily_save")}
              </button>
              <button className="btn btn-sm btn-ghost" onClick={() => setShowInput(false)}>{t("skills.tavily_cancel")}</button>
            </div>
          )}
        </div>
      )}

      <div style={{ marginTop: 20, padding: 14, background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-lg)" }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>{t("skills.new")}</div>
        <input className="form-input" style={{ fontSize: 11, marginBottom: 6 }} placeholder={t("skills.name_placeholder")}
          value={newSkill.name} onChange={e => setNewSkill({ ...newSkill, name: e.target.value })} />
        <textarea className="form-input" style={{ fontSize: 11, minHeight: 80, resize: "vertical", fontFamily: "var(--font-mono)" }}
          placeholder={t("skills.content_placeholder")}
          value={newSkill.content} onChange={e => setNewSkill({ ...newSkill, content: e.target.value })} />
        <button className="btn btn-sm btn-primary" onClick={addSkill}>{t("skills.create")}</button>
      </div>
    </>
  );
}
