import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "../i18n";
import { authFetch } from "../utils/api";

interface SettingsViewProps {
  theme: "light" | "dark";
  setTheme: (t: "light" | "dark") => void;
  sidecarStatus: string;
  restartingSidecar: boolean;
  onRestartSidecar: () => void;
  gatewayLogsOpen: boolean;
  setGatewayLogsOpen: (v: boolean) => void;
  gatewayLogs: { time: string; level: string; message: string }[];
  selectedModel: string;
  cloudModels: { name: string }[];
  setActiveView: (v: string) => void;
  autoLaunch: boolean;
  setAutoLaunch: (v: boolean) => void;
  autoStartGateway: boolean;
  setAutoStartGateway: (v: boolean) => void;
  anonymousData: boolean;
  setAnonymousData: (v: boolean) => void;
  autoCheckUpdate: boolean;
  setAutoCheckUpdate: (v: boolean) => void;
  appVersion: string;
  checkingUpdate: boolean;
  onCheckUpdate: () => void;
  reflectionMode: "off" | "light" | "deep";
  setReflectionMode: (v: "off" | "light" | "deep") => void;
  /** 朗读：开关 / 语速 / 音色（音色列表来自系统语音，随设备变化） */
  ttsEnabled: boolean;
  setTtsEnabled: (v: boolean) => void;
  ttsRate: number;
  setTtsRate: (v: number) => void;
  ttsVoice: string;
  setTtsVoice: (v: string) => void;
  ttsVoices: { name: string; lang: string }[];
}

function toggleOnChange(setter: (v: boolean) => void, storageKey: string) {
  return (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = e.target.checked;
    setter(v);
    localStorage.setItem(storageKey, String(v));
  };
}

/** 人格与称呼：显示当前的 称呼/名字/语气，并可重新运行首启引导。 */
function PersonaCard() {
  const { t } = useTranslation();
  const [info, setInfo] = useState<{ done: boolean; user_name: string; agent_name: string; tone: string } | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await authFetch("/v1/onboarding");
      const d = await r.json();
      setInfo({
        done: !!d.done,
        user_name: d.user_name || "",
        agent_name: d.agent_name || "",
        tone: d.tone || "",
      });
    } catch { /* 侧车未就绪时卡片留空 */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  const post = async (path: string, okKey: string) => {
    setBusy(true);
    try {
      await authFetch(path, { method: "POST" });
      setNote(t(okKey));
      await load();
    } catch {
      setNote(t("local.failed"));
    } finally {
      setBusy(false);
    }
  };

  const val = (v: string) => v || t("settings.persona_unset");
  const rows: [string, string][] = [
    [t("settings.persona_user"), val(info?.user_name || "")],
    [t("settings.persona_agent"), val(info?.agent_name || "")],
    [t("settings.persona_tone"), val(info?.tone || "")],
  ];

  return (
    <div className="settings-group">
      <div className="settings-group-header">{t("settings.persona_title")}</div>
      {rows.map(([label, value]) => (
        <div className="settings-row" key={label}>
          <div><div className="settings-row-label">{label}</div></div>
          <span style={{ fontSize: 12, color: value === t("settings.persona_unset") ? "var(--text-muted)" : "var(--text-primary)" }}>{value}</span>
        </div>
      ))}
      <div className="settings-row" style={{ alignItems: "flex-start" }}>
        <div>
          <div className="settings-row-desc" style={{ maxWidth: 380 }}>{t("settings.persona_desc")}</div>
          {info && !info.done && (
            <div style={{ fontSize: 11, color: "var(--warning)", marginTop: 6 }}>{t("settings.persona_pending")}</div>
          )}
          {note && <div style={{ fontSize: 11, color: "var(--success)", marginTop: 6 }}>{note}</div>}
        </div>
        <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
          <button className="btn btn-sm btn-ghost" disabled={busy}
            onClick={() => post("/v1/onboarding/reset", "settings.persona_reset_done")}>
            {t("settings.persona_rerun")}
          </button>
          <button className="btn btn-sm btn-ghost" disabled={busy}
            onClick={() => post("/v1/onboarding/complete", "settings.persona_skipped")}>
            {t("settings.persona_skip")}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function SettingsView({
  theme, setTheme,
  sidecarStatus, restartingSidecar, onRestartSidecar,
  gatewayLogsOpen, setGatewayLogsOpen, gatewayLogs,
  selectedModel, cloudModels, setActiveView,
  autoLaunch, setAutoLaunch, autoStartGateway, setAutoStartGateway,
  anonymousData, setAnonymousData, autoCheckUpdate, setAutoCheckUpdate,
  appVersion, checkingUpdate, onCheckUpdate,
  reflectionMode, setReflectionMode,
  ttsEnabled, setTtsEnabled, ttsRate, setTtsRate, ttsVoice, setTtsVoice, ttsVoices,
}: SettingsViewProps) {
  const { t, lang, setLanguage } = useTranslation();

  const reflectionOptions: { value: "off" | "light" | "deep"; label: string }[] = [
    { value: "off", label: t("settings.reflection_off") },
    { value: "light", label: t("settings.reflection_light") },
    { value: "deep", label: t("settings.reflection_deep") },
  ];

  return (
    <div className="page-body">
      <div style={{ maxWidth: 620 }}>

              <div className="settings-group">
        <div className="settings-group-header">{t("settings.reflection_title")}</div>
        <div style={{ padding: "10px 16px" }}>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            {reflectionOptions.map((o) => (
              <button key={o.value} className={`btn btn-sm ${reflectionMode === o.value ? "btn-primary" : "btn-ghost"}`}
                onClick={() => { setReflectionMode(o.value); localStorage.setItem("latiao_reflection", o.value); }}>
                {o.label}
              </button>
            ))}
          </div>
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 6 }}>{t("settings.reflection_desc")}</div>
        </div>
      </div>

<div className="settings-group">
          <div className="settings-group-header">{t("settings.general")}</div>
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.theme")}</div><div className="settings-row-desc">{t("settings.theme_desc")}</div></div>
            <div className="segmented">
              <button className={`segmented-btn${theme === "light" ? " active" : ""}`} onClick={() => setTheme("light")}>{t("settings.theme_light")}</button>
              <button className={`segmented-btn${theme === "dark" ? " active" : ""}`} onClick={() => setTheme("dark")}>{t("settings.theme_dark")}</button>
            </div>
          </div>
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.language")}</div><div className="settings-row-desc">{t("settings.language_desc")}</div></div>
            <select className="form-input" style={{ width: "auto", margin: 0, padding: "5px 10px", fontSize: 11 }}
              value={lang} onChange={e => { setLanguage(e.target.value as "zh" | "en" | "ja" | "ru"); }}>
              <option value="zh">{t("settings.lang_zh")}</option>
              <option value="en">{t("settings.lang_en")}</option>
              <option value="ja">{t("settings.lang_ja")}</option>
              <option value="ru">{t("settings.lang_ru")}</option>
            </select>
          </div>
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.auto_launch")}</div><div className="settings-row-desc">{t("settings.auto_launch_desc")}</div></div>
            <label className="toggle">
              <input type="checkbox" checked={autoLaunch} onChange={toggleOnChange(setAutoLaunch, "latiao_auto_launch")} />
              <span className="toggle-slider"></span>
            </label>
          </div>
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.tts")}</div><div className="settings-row-desc">{t("settings.tts_desc")}</div></div>
            <label className="toggle">
              <input type="checkbox" checked={ttsEnabled} onChange={toggleOnChange(setTtsEnabled, "latiao_tts")} />
              <span className="toggle-slider"></span>
            </label>
          </div>
          {ttsEnabled && (
            <>
              <div className="settings-row">
                <div><div className="settings-row-label">{t("settings.tts_rate")}</div></div>
                <select className="form-input" style={{ width: "auto", margin: 0, padding: "5px 10px", fontSize: 11 }}
                  value={String(ttsRate)}
                  onChange={e => { const v = Number(e.target.value) || 1; setTtsRate(v); localStorage.setItem("latiao_tts_rate", String(v)); }}>
                  <option value="0.75">0.75×</option>
                  <option value="1">1×</option>
                  <option value="1.25">1.25×</option>
                  <option value="1.5">1.5×</option>
                </select>
              </div>
              <div className="settings-row">
                <div>
                  <div className="settings-row-label">{t("settings.tts_voice")}</div>
                  <div className="settings-row-desc">
                    {ttsVoices.length ? t("settings.tts_voice_desc", { count: ttsVoices.length }) : t("settings.tts_voice_none")}
                  </div>
                </div>
                <select className="form-input" style={{ width: "auto", margin: 0, padding: "5px 10px", fontSize: 11, maxWidth: 200 }}
                  value={ttsVoice}
                  onChange={e => { setTtsVoice(e.target.value); localStorage.setItem("latiao_tts_voice", e.target.value); }}>
                  <option value="">{t("settings.tts_voice_default")}</option>
                  {ttsVoices.map(v => (
                    <option key={v.name + v.lang} value={v.name}>{`${v.name} · ${v.lang}`}</option>
                  ))}
                </select>
              </div>
            </>
          )}
        </div>

        <div className="settings-group">
          <div className="settings-group-header">{t("settings.gateway")}</div>
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.status")}</div><div className="settings-row-desc">{t("settings.status_desc")}</div></div>
            <div className="settings-row-right">
              <span className={`status-dot ${sidecarStatus === "online" ? "online" : "offline"}`}></span>
              <span style={{ fontSize: 12, color: sidecarStatus === "online" ? "var(--success)" : "var(--danger)", fontWeight: 500 }}>
                {sidecarStatus === "online" ? t("sidebar.online") : t("sidebar.offline")}
              </span>
            </div>
          </div>
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.port")}</div><div className="settings-row-desc">{t("settings.port_desc")}</div></div>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-muted)" }}>8765</span>
          </div>
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.restart")}</div><div className="settings-row-desc">{t("settings.restart_desc")}</div></div>
            <button className="btn btn-sm btn-ghost"
              onClick={onRestartSidecar}
              disabled={restartingSidecar}>
              {restartingSidecar ? "⏳" : "🔄"} {restartingSidecar ? "重启中..." : t("settings.restart")}
            </button>
          </div>
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.logs")}</div><div className="settings-row-desc">{t("settings.logs_desc")}</div></div>
            <button className="btn btn-sm btn-ghost" onClick={() => setGatewayLogsOpen(!gatewayLogsOpen)}>{gatewayLogsOpen ? t("settings.logs_hide") : t("settings.logs_show")}</button>
          </div>
          {gatewayLogsOpen && (
            <div className="log-panel" style={{ display: "block", margin: "0 16px 12px", maxHeight: 300, overflowY: "auto" }}>
              {gatewayLogs.length === 0 ? (
                <div style={{ color: "var(--text-muted)" }}>{t("settings.no_logs")}</div>
              ) : (
                gatewayLogs.map((entry, i) => {
                  const color = entry.level === "ERROR" ? "var(--danger)" :
                    entry.level === "WARNING" ? "var(--warning)" :
                    entry.level === "INFO" ? "var(--success)" : "var(--text-muted)";
                  return (
                    <div key={i} style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>
                      [{entry.time}] <span style={{ color }}>{entry.level.padEnd(7)}</span> {entry.message}
                    </div>
                  );
                })
              )}
            </div>
          )}
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.auto_gateway")}</div><div className="settings-row-desc">{t("settings.auto_gateway_desc")}</div></div>
            <label className="toggle">
              <input type="checkbox" checked={autoStartGateway} onChange={toggleOnChange(setAutoStartGateway, "latiao_auto_gateway")} />
              <span className="toggle-slider"></span>
            </label>
          </div>
        </div>

        <div className="settings-group">
          <div className="settings-group-header">{t("settings.anonymous")}</div>
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.anonymous")}</div><div className="settings-row-desc">{t("settings.anonymous_desc")}</div></div>
            <label className="toggle">
              <input type="checkbox" checked={anonymousData} onChange={toggleOnChange(setAnonymousData, "latiao_anonymous_data")} />
              <span className="toggle-slider"></span>
            </label>
          </div>
        </div>

        <div className="settings-group">
          <div className="settings-group-header">{t("settings.updates")}</div>
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.version")}</div><div className="settings-row-desc">{t("settings.version_desc")}</div></div>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-muted)" }}>v{appVersion}</span>
          </div>
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.auto_update")}</div><div className="settings-row-desc">{t("settings.auto_update_desc")}</div></div>
            <label className="toggle">
              <input type="checkbox" checked={autoCheckUpdate} onChange={toggleOnChange(setAutoCheckUpdate, "latiao_auto_check_update")} />
              <span className="toggle-slider"></span>
            </label>
          </div>
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.check_update")}</div><div className="settings-row-desc">{t("settings.check_update_desc")}</div></div>
            <button className="btn-secondary" onClick={onCheckUpdate} disabled={checkingUpdate}>
              {checkingUpdate ? t("settings.checking") : t("settings.check_now")}
            </button>
          </div>
        </div>

        <div className="settings-group">
          <div className="settings-group-header">{t("settings.model_config")}</div>
          <div className="settings-row">
            <div>
              <div className="settings-row-label">{t("settings.main_agent")}</div>
              <div className="settings-row-desc" style={{ fontFamily: "var(--font-mono)" }}>
                {selectedModel || t("settings.auto_engine")}
              </div>
            </div>
            <button className="btn btn-sm btn-ghost" onClick={() => setActiveView("models")}>
              {t("settings.go_models")}
            </button>
          </div>
          {cloudModels.length > 0 && (
            <div className="settings-row">
              <div>
                <div className="settings-row-label">{t("settings.models_count", { count: cloudModels.length })}</div>
                <div className="settings-row-desc">{cloudModels.map(m => m.name).join(", ")}</div>
              </div>
            </div>
          )}
        </div>

        <PersonaCard />

      </div>
    </div>
  );
}
