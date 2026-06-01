import { useState } from "react";
import { useTranslation } from "../i18n";

const CHANNELS = [
  { name: "Telegram", icon: "💬", key: "telegram", color: "var(--accent)", placeholder: "Bot Token (from @BotFather)" },
  { name: "Discord", icon: "🎮", key: "discord", color: "#5865f2", placeholder: "Bot Token (from Developer Portal)" },
  { name: "WhatsApp", icon: "📱", key: "whatsapp", color: "#25d366", placeholder: "Phone number ID" },
  { name: "WeChat", icon: "💚", key: "wechat", color: "#07c160", placeholder: "Plugin endpoint" },
  { name: "DingTalk", icon: "📌", key: "dingtalk", color: "#0089ff", placeholder: "App Key" },
  { name: "WeCom", icon: "🏢", key: "wecom", color: "#07c160", placeholder: "Webhook URL" },
  { name: "QQ Bot", icon: "🐧", key: "qq", color: "#12b7f5", placeholder: "Bot AppID" },
  { name: "Feishu / Lark", icon: "🪶", key: "feishu", color: "#3370ff", placeholder: "App ID" },
];

export default function ChannelsView() {
  const { t } = useTranslation();
  const [configs, setConfigs] = useState<Record<string, string>>(() => {
    try {
      return JSON.parse(localStorage.getItem("latiao_channels") || "{}");
    } catch { return {}; }
  });
  const [activeChannel, setActiveChannel] = useState<string | null>(null);

  const saveConfig = (key: string, value: string) => {
    const next = { ...configs, [key]: value };
    setConfigs(next);
    localStorage.setItem("latiao_channels", JSON.stringify(next));
  };

  const clearConfig = (key: string) => {
    const next = { ...configs };
    delete next[key];
    setConfigs(next);
    localStorage.setItem("latiao_channels", JSON.stringify(next));
  };

  return (
    <div className="page-body">
      <div className="card-grid">
        {CHANNELS.map((ch) => {
          const isConfigured = !!configs[ch.key];
          const isActive = activeChannel === ch.key;
          return (
            <div key={ch.key} className="card" style={isConfigured ? { borderLeft: "2px solid " + ch.color } : { opacity: 0.7 }}>
              <div className="card-title">
                <span style={{ fontSize: 18 }}>{ch.icon}</span> {ch.name}
                <span className={`badge ${isConfigured ? "badge-active" : "badge-inactive"}`} style={{ marginLeft: 6 }}>
                  {isConfigured ? t("channels.configured") : t("channels.not_configured")}
                </span>
              </div>
              <div className="card-desc">{t("channels." + ch.key + "_desc")}</div>

              {isActive ? (
                <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
                  <input
                    className="form-input"
                    style={{ margin: 0, fontSize: 11, padding: "6px 10px", fontFamily: "var(--font-mono)" }}
                    placeholder={ch.placeholder}
                    value={configs[ch.key] || ""}
                    onChange={(e) => saveConfig(ch.key, e.target.value)}
                  />
                  <div style={{ display: "flex", gap: 6 }}>
                    <button className="btn btn-sm btn-primary" style={{ flex: 1 }}
                      onClick={() => setActiveChannel(null)}>
                      {t("channels.done")}
                    </button>
                    {isConfigured && (
                      <button className="btn btn-sm btn-ghost" style={{ color: "var(--danger)" }}
                        onClick={() => { clearConfig(ch.key); setActiveChannel(null); }}>
                        {t("channels.clear")}
                      </button>
                    )}
                  </div>
                </div>
              ) : (
                <button className="btn btn-sm btn-primary" style={{ marginTop: 10, width: "100%" }}
                  onClick={() => setActiveChannel(ch.key)}>
                  {isConfigured ? t("channels.edit") : t("channels.connect")}
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
