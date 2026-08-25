import { useTranslation } from "../i18n";
import { useEffect, useState } from "react";

interface RecoveryViewProps {
  sidecarStatus: "checking" | "online" | "offline";
  restartingSidecar: boolean;
  onRestartSidecar: () => void;
  gatewayLogs: { time: string; level: string; message: string }[];
  fetchLogs: () => void;
}

export default function RecoveryView({
  sidecarStatus, restartingSidecar, onRestartSidecar,
  gatewayLogs, fetchLogs,
}: RecoveryViewProps) {
  const { t } = useTranslation();
  const [logFilter, setLogFilter] = useState("");

  useEffect(() => { fetchLogs(); }, [sidecarStatus]); // eslint-disable-line react-hooks/exhaustive-deps

  const filtered = logFilter
    ? gatewayLogs.filter(l => l.message.toLowerCase().includes(logFilter.toLowerCase()))
    : gatewayLogs;

  return (
    <div style={{
      minHeight: "100vh", display: "flex", flexDirection: "column",
      alignItems: "center", justifyContent: "center",
      background: "var(--bg)", padding: 24, gap: 16,
    }}>
      <div style={{
        maxWidth: 520, width: "100%", background: "var(--bg-card)",
        border: "1px solid var(--border-default)", borderRadius: 16, padding: 28,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
          <span className={`status-dot ${sidecarStatus === "online" ? "online" : "offline"}`} style={{ width: 12, height: 12 }} />
          <div style={{ fontSize: 16, fontWeight: 650 }}>{t("recovery.title")}</div>
        </div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 20 }}>
          {t("recovery.desc")}
        </div>

        {/* 诊断状态 */}
        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 20 }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
            <span>{t("recovery.diag_sidecar")}</span>
            <span style={{ color: sidecarStatus === "online" ? "var(--success, #34d399)" : "var(--danger, #f87171)" }}>
              {sidecarStatus === "online" ? t("recovery.ok") : sidecarStatus === "checking" ? t("recovery.checking") : t("recovery.failed")}
            </span>
          </div>
        </div>

        {/* 操作按钮 */}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
          <button className="btn btn-primary" disabled={restartingSidecar}
            onClick={onRestartSidecar}>
            {restartingSidecar ? "…" : t("recovery.restart")}
          </button>
          <button className="btn btn-ghost" onClick={fetchLogs}>{t("recovery.refresh")}</button>
        </div>

        {/* 日志 */}
        <div style={{ marginBottom: 8, display: "flex", alignItems: "center", gap: 8 }}>
          <input className="form-input" style={{ flex: 1, minWidth: 0, fontSize: 11 }}
            placeholder={t("recovery.log_filter")} value={logFilter}
            onChange={e => setLogFilter(e.target.value)} />
        </div>
        <div style={{
          maxHeight: 220, overflowY: "auto", fontSize: 10.5, fontFamily: "var(--font-mono)",
          background: "var(--bg)", borderRadius: 8, padding: 10, border: "1px solid var(--border-default)",
        }}>
          {filtered.length === 0 ? (
            <div style={{ color: "var(--text-muted)" }}>{t("recovery.no_logs")}</div>
          ) : (
            filtered.slice(-60).reverse().map((l, i) => (
              <div key={i} style={{
                whiteSpace: "pre-wrap", wordBreak: "break-all",
                color: l.level === "ERROR" ? "var(--danger, #f87171)" :
                       l.level === "WARNING" ? "var(--warning, #fbbf24)" : "var(--text-muted)",
              }}>
                <span style={{ color: "var(--text-muted)", opacity: 0.7 }}>{l.time} </span>
                {l.message}
              </div>
            ))
          )}
        </div>

        <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 16, lineHeight: 1.6 }}>
          {t("recovery.hint")}
        </div>
      </div>
    </div>
  );
}
