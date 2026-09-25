import { useState } from "react";
import type { CloudModel } from "../types";
import { useTranslation } from "../i18n";

function detectProvider(name: string) {
  const n = name.toLowerCase();
  if (n.includes("deepseek")) return { protocol: "openai", endpoint: "https://api.deepseek.com/v1", provider: "DeepSeek" };
  if (n.includes("gpt") || n.includes("o1") || n.includes("o3") || n.includes("openai")) return { protocol: "openai", endpoint: "https://api.openai.com/v1", provider: "OpenAI" };
  if (n.includes("claude") || n.includes("anthropic")) return { protocol: "anthropic", endpoint: "https://api.anthropic.com", provider: "Anthropic" };
  if (n.includes("gemini") || n.includes("google")) return { protocol: "openai", endpoint: "https://generativelanguage.googleapis.com/v1beta/openai", provider: "Google" };
  return null;
}

interface Props {
  selectedModel: string;
  setSelectedModel: (m: string) => void;
  cloudModels: CloudModel[];
  setCloudModels: React.Dispatch<React.SetStateAction<CloudModel[]>>;
  newCloudModel: CloudModel;
  setNewCloudModel: React.Dispatch<React.SetStateAction<CloudModel>>;
  showAdvanced: boolean;
  setShowAdvanced: (v: boolean) => void;
  testingModel: string | null;
  testResult: string;
  testConnection: (modelName: string, key: string | undefined, endpoint: string, protocol: string) => void;
  recentLearnings: { topic: string; content: string; confidence: number }[];
  showToast: (msg: string, type?: string) => void;
}

export default function CloudModelsTab({
  selectedModel, setSelectedModel,
  cloudModels, setCloudModels, newCloudModel, setNewCloudModel,
  showAdvanced, setShowAdvanced,
  testingModel, testResult, testConnection,
  recentLearnings, showToast,
}: Props) {
  const { t } = useTranslation();
  const [editingKeyIdx, setEditingKeyIdx] = useState<number | null>(null);
  const [editKeyVal, setEditKeyVal] = useState("");
  const updateKey = async (idx: number) => {
    const val = (editKeyVal || "").trim();
    if (!val) { showToast(t("cloud.fill_required"), "warn"); return; }
    const withKey = cloudModels.map((x, j) => j === idx ? { ...x, key: val } : { ...x, key: x.key || "" });
    try {
      const { sidecarFetchWithRetry } = await import("../utils/api");
      await sidecarFetchWithRetry("/v1/settings/cloud-models", "POST", { models: withKey }, 2);
      setCloudModels(prev => prev.map((x, j) => j === idx ? { ...x, has_key: true, key: undefined } : x));
      setEditingKeyIdx(null);
      setEditKeyVal("");
      showToast(t("cloud.key_updated"));
    } catch (e) {
      console.error(e);
      // 失败：key 仍在输入框，不许先清再靠 debounce 重试（那时 key 已丢）
      showToast(t("cloud.fill_required"), "warn");
    }
  };
  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8 }}>
        {t("cloud.desc")}
      </div>
      <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 16 }}>
        🔒 {t("cloud.key_note")}
      </div>

      <div className="settings-group" style={{ marginBottom: 16 }}>
        <div className="settings-row">
          <div>
            <div className="settings-row-label">{t("cloud.main_agent")}</div>
            <div className="settings-row-desc" style={{ fontFamily: "var(--font-mono)" }}>
              {selectedModel || t("cloud.auto_detect")}
            </div>
          </div>
          {selectedModel && (
            <button className="btn btn-sm btn-ghost" onClick={() => setSelectedModel("")}>{t("cloud.deselect")}</button>
          )}
        </div>
      </div>

      {cloudModels.map((m, i) => {
        const isMain = selectedModel === m.name;
        const hasImage = m.name.includes("gpt-4o") || m.name.includes("claude") || m.name.includes("gemini");
        return (
        <div key={i} className="settings-group" style={{ marginBottom: 8 }}>
          <div className="settings-group-header">{m.name} {isMain && <span className="badge badge-active">main agent</span>}</div>
          <div className="settings-row">
            <div>
              <div className="settings-row-desc" style={{ fontFamily: "var(--font-mono)" }}>
                {m.protocol || "openai"} · {m.endpoint}
              </div>
              <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
                API Key: {(m.has_key || m.key) ? t("cloud.key_saved") : t("cloud.key_missing")}
                {hasImage && <span> · 📷 {t("cloud.img_support")}</span>}
              </div>
            </div>
            <div style={{ display: "flex", gap: 4, flexShrink: 0 }}>
              {!isMain && (
                <button className="btn btn-sm btn-primary" onClick={() => { setSelectedModel(m.name); showToast(t("cloud.set_agent", { name: m.name })); }}>
                  {t("cloud.set_main")}
                </button>
              )}
              <button className="btn btn-sm btn-ghost" onClick={() => { setEditingKeyIdx(editingKeyIdx === i ? null : i); setEditKeyVal(""); }}>
                {t("cloud.update_key")}
              </button>
              <button className="btn btn-sm btn-ghost" onClick={() => testConnection(m.name, undefined, m.endpoint, m.protocol || "openai")} disabled={testingModel === m.name}>
                {testingModel === m.name ? t("cloud.testing") : t("cloud.test")}
              </button>
              <button className="btn-icon" style={{ fontSize: 14 }} onClick={() => {
                const next = cloudModels.filter((_, j) => j !== i);
                setCloudModels(next);
                // 删到空表要显式 clear，否则后端拒绝整表清空
                if (next.length === 0) {
                  void import("../utils/api").then(({ sidecarFetchWithRetry }) =>
                    sidecarFetchWithRetry("/v1/settings/cloud-models", "POST", { models: [], clear: true }, 2)
                  ).catch(() => { /* debounce 也会重试 */ });
                }
                if (isMain) setSelectedModel("");
                showToast(t("cloud.removed", { name: m.name }));
              }}>✕</button>
            </div>
          </div>
          {editingKeyIdx === i && (
            <div className="settings-row" style={{ gap: 6 }}>
              <input className="form-input" type="password" style={{ flex: 1, margin: 0, fontSize: 11, padding: "6px 10px" }}
                placeholder={t("cloud.update_key_hint")}
                value={editKeyVal}
                onChange={(e) => setEditKeyVal(e.target.value)}
                autoFocus
                onKeyDown={(e) => { if (e.key === "Enter") void updateKey(i); }} />
              <button className="btn btn-sm btn-primary" style={{ flex: "0 0 auto" }} onClick={() => void updateKey(i)}>{t("cloud.add_btn")}</button>
              <button className="btn btn-sm btn-ghost" style={{ flex: "0 0 auto" }} onClick={() => { setEditingKeyIdx(null); setEditKeyVal(""); }}>✕</button>
            </div>
          )}
          <div className="settings-row">
            <span style={{ fontSize: 10, color: "var(--text-muted)" }}>max_tokens:</span>
            <input className="form-input" style={{ width: 80, margin: 0, padding: "4px 8px", fontSize: 11, textAlign: "center", fontFamily: "var(--font-mono)" }}
              value={m.max_tokens || 32768}
              onChange={e => {
                const v = parseInt(e.target.value) || 32768;
                setCloudModels(prev => prev.map((x, j) => j === i ? { ...x, max_tokens: v } : x));
              }} />
          </div>
        </div>
      )})}
      {testResult && <div style={{ fontSize: 11, marginBottom: 12, color: testResult.includes("✅") ? "var(--success)" : "var(--danger)" }}>{testResult}</div>}

      <div className="settings-group">
        <div className="settings-group-header">{t("cloud.add_model")}</div>
        <div style={{ padding: "12px 16px" }}>
          <div style={{ display: "flex", gap: 8, marginBottom: 8, flexWrap: "wrap" }}>
            <input className="form-input" style={{ flex: "1 1 140px", margin: 0, fontSize: 11, padding: "6px 10px" }}
              placeholder={t("cloud.model_id")} value={newCloudModel.name}
              onChange={(e) => {
                const name = e.target.value;
                const auto = detectProvider(name);
                setNewCloudModel(prev => ({ ...prev, name, ...(auto ? { protocol: auto.protocol, endpoint: auto.endpoint } : {}) }));
              }} />
            <input className="form-input" style={{ flex: "1 1 180px", margin: 0, fontSize: 11, padding: "6px 10px" }}
              placeholder="API Key" type="password" value={newCloudModel.key}
              onChange={(e) => setNewCloudModel({ ...newCloudModel, key: e.target.value })} />
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
            <input className="form-input" style={{ width: 72, margin: 0, padding: "4px 8px", fontSize: 11, textAlign: "center", fontFamily: "var(--font-mono)" }}
              placeholder="tokens" value={newCloudModel.max_tokens || 32768}
              onChange={e => setNewCloudModel({ ...newCloudModel, max_tokens: parseInt(e.target.value) || 32768 })} />
            <button className="btn btn-sm btn-primary" style={{ minWidth: 96, flex: "0 0 auto" }} onClick={() => {
              if (!newCloudModel.name || !newCloudModel.key) { showToast(t("cloud.fill_required"), "warn"); return; }
              const auto = detectProvider(newCloudModel.name);
              const m = { ...newCloudModel };
              if (auto && !m.endpoint) { m.protocol = auto.protocol; m.endpoint = auto.endpoint; }
              // key 必须随本次 POST 成功后才能从内存清掉——失败时 debounce 会拿空 key
              // 再写一遍（服务端沿用旧值）→ 新模型以空 key 入库且界面谎报已保存。
              const withKey = [...cloudModels.map((x) => ({ ...x, key: x.key || "" })), { ...m }];
              void (async () => {
                try {
                  const { sidecarFetchWithRetry } = await import("../utils/api");
                  await sidecarFetchWithRetry("/v1/settings/cloud-models", "POST", { models: withKey }, 2);
                  setCloudModels((prev) => [...prev, { ...m, has_key: true, key: undefined }]);
                  setNewCloudModel({ name: "", key: "", endpoint: "", protocol: "openai", max_tokens: 32768 });
                  showToast(t("cloud.added", { name: m.name }));
                } catch (e) {
                  console.error(e);
                  showToast(t("cloud.fill_required"), "warn");
                }
              })();
            }}>{t("cloud.add_btn")}</button>
          </div>
          <div style={{ fontSize: 10, color: "var(--text-muted)" }}>
            {t("cloud.auto_detect_note")}
            {newCloudModel.protocol && newCloudModel.endpoint && (
              <span style={{ marginLeft: 4, fontFamily: "var(--font-mono)", color: "var(--accent)" }}>
                {t("cloud.detected", { protocol: newCloudModel.protocol, endpoint: newCloudModel.endpoint })}
              </span>
            )}
          </div>
          <button className="btn btn-sm btn-ghost" style={{ marginTop: 8, fontSize: 10 }}
            onClick={() => setShowAdvanced(!showAdvanced)}>
            {showAdvanced ? t("cloud.advanced_hide") : t("cloud.advanced_show")}{" "}{t("cloud.advanced_title")}
          </button>
          {showAdvanced && (
            <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
              <select className="form-input" style={{ width: "auto", margin: 0, padding: "4px 8px", fontSize: 11 }}
                value={newCloudModel.protocol} onChange={e => setNewCloudModel({ ...newCloudModel, protocol: e.target.value })}>
                <option value="openai">OpenAI</option>
                <option value="anthropic">Anthropic</option>
              </select>
              <input className="form-input" style={{ flex: 1, margin: 0, fontSize: 11, padding: "4px 8px", fontFamily: "var(--font-mono)" }}
                placeholder={t("cloud.add_model")} value={newCloudModel.endpoint}
                onChange={e => setNewCloudModel({ ...newCloudModel, endpoint: e.target.value })} />
            </div>
          )}
        </div>
      </div>

      <div style={{ fontSize: 13, fontWeight: 600, margin: "20px 0 8px" }}>{t("cloud.knowledge")}</div>
      {recentLearnings.length > 0 ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {recentLearnings.slice(0, 5).map((l, i) => (
            <div key={i} style={{ background: "var(--bg-card)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-md)", padding: "8px 12px", display: "flex", gap: 10, alignItems: "center" }}>
              <span style={{ fontSize: 12 }}>{l.confidence > 0.7 ? "🧠" : "📝"}</span>
              <div style={{ flex: 1, minWidth: 0, fontSize: 11 }}>{l.topic || t("common.knowledge")}: {(l.content || "").slice(0, 80)}{(l.content || "").length > 80 ? "..." : ""}</div>
              <span style={{ fontSize: 10, color: "var(--text-muted)" }}>{Math.round((l.confidence || 0) * 100)}%</span>
            </div>
          ))}
        </div>
      ) : (
        <div style={{ textAlign: "center", padding: 16, color: "var(--text-muted)", fontSize: 11 }}>
          🧠 {t("cloud.knowledge_empty")}
        </div>
      )}
    </div>
  );
}
