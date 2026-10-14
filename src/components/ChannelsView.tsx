import { useState, useEffect } from "react";
import { invoke } from "@tauri-apps/api/core";
import { useTranslation } from "../i18n";

const CHANNEL_KEYCHAIN_KEY = "channel_tokens";

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
  const [configs, setConfigs] = useState<Record<string, boolean>>({});
  // 编辑框明文只在打字期间存在，保存后立刻清空
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [activeChannel, setActiveChannel] = useState<string | null>(null);

  // 只查「是否已配置」，不把明文 token 读进 webview（密钥代理化）
  useEffect(() => {
    (async () => {
      try {
        // 旧版整包键 channel_tokens → 新版 channel_tokens:<channel>
        await invoke("migrate_channel_tokens").catch(() => 0);
        const map: Record<string, boolean> = {};
        for (const ch of CHANNELS) {
          const ok = await invoke("has_secret", { key: CHANNEL_KEYCHAIN_KEY + ":" + ch.key }).catch(() => false) as boolean;
          if (ok) map[ch.key] = true;
        }
        setConfigs(map);
      } catch (e) { console.warn("Failed to load channel secret flags", e); }
    })();
  }, []);

  const saveConfig = async (key: string) => {
    const value = (drafts[key] || "").trim();
    if (!value) return;
    try {
      await invoke("store_secret", { key: CHANNEL_KEYCHAIN_KEY + ":" + key, value });
      setConfigs(prev => ({ ...prev, [key]: true }));
      setDrafts(prev => ({ ...prev, [key]: "" }));
      setActiveChannel(null);
    } catch (e) { console.warn("Failed to store channel token", e); }
  };

  const clearConfig = async (key: string) => {
    try {
      await invoke("delete_secret", { key: CHANNEL_KEYCHAIN_KEY + ":" + key });
      setConfigs(prev => { const n = { ...prev }; delete n[key]; return n; });
      setDrafts(prev => ({ ...prev, [key]: "" }));
      setActiveChannel(null);
    } catch (e) { console.warn("Failed to delete channel token", e); }
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
                    type="password"
                    className="form-input"
                    style={{ margin: 0, fontSize: 11, padding: "6px 10px", fontFamily: "var(--font-mono)" }}
                    placeholder={ch.placeholder}
                    value={drafts[ch.key] || ""}
                    onChange={(e) => setDrafts(prev => ({ ...prev, [ch.key]: e.target.value }))}
                  />
                  <div style={{ display: "flex", gap: 6 }}>
                    <button className="btn btn-sm btn-primary" style={{ flex: 1 }}
                      onClick={() => saveConfig(ch.key)}>
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
