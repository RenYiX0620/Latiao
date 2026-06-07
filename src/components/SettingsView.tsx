import { useTranslation } from "../i18n";

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
}

function toggleOnChange(setter: (v: boolean) => void, storageKey: string) {
  return (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = e.target.checked;
    setter(v);
    localStorage.setItem(storageKey, String(v));
  };
}

export default function SettingsView({
  theme, setTheme,
  sidecarStatus, restartingSidecar, onRestartSidecar,
  gatewayLogsOpen, setGatewayLogsOpen, gatewayLogs,
  selectedModel, cloudModels, setActiveView,
  autoLaunch, setAutoLaunch, autoStartGateway, setAutoStartGateway,
  anonymousData, setAnonymousData, autoCheckUpdate, setAutoCheckUpdate,
}: SettingsViewProps) {
  const { t, lang, setLanguage } = useTranslation();

  return (
    <div className="page-body">
      <div style={{ maxWidth: 620 }}>

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
        </div>

        <div className="settings-group">
          <div className="settings-group-header">{t("settings.gateway")}</div>
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.status")}</div><div className="settings-row-desc">{t("settings.status_desc")}</div></div>
            <div className="settings-row-right">
              <span className={`status-dot ${sidecarStatus === "online" ? "online" : "offline"}`}></span>
              <span style={{ fontSize: 12, color: sidecarStatus === "online" ? "var(--success)" : "var(--danger)", fontWeight: 500 }}>
                {sidecarStatus === "online" ? t("settings.status_running") : t("settings.status_offline")}
              </span>
            </div>
          </div>
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.port")}</div><div className="settings-row-desc">{t("settings.port_desc")}</div></div>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-muted)" }}>8000</span>
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
            <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-muted)" }}>v0.1.0</span>
          </div>
          <div className="settings-row">
            <div><div className="settings-row-label">{t("settings.auto_update")}</div><div className="settings-row-desc">{t("settings.auto_update_desc")}</div></div>
            <label className="toggle">
              <input type="checkbox" checked={autoCheckUpdate} onChange={toggleOnChange(setAutoCheckUpdate, "latiao_auto_check_update")} />
              <span className="toggle-slider"></span>
            </label>
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

      </div>
    </div>
  );
}
