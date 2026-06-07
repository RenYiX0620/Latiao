import { useState, useMemo, useCallback } from "react";
import { useTranslation } from "../i18n";

interface LogEntry {
  time: string;
  level: string;
  message: string;
}

interface LogsViewProps {
  logs: LogEntry[];
}

const LEVEL_COLORS: Record<string, string> = {
  ERROR: "var(--danger)",
  WARNING: "var(--warning)",
  INFO: "var(--success)",
};

export default function LogsView({ logs }: LogsViewProps) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState<string>("ALL");
  const [autoScroll, setAutoScroll] = useState(true);

  const filtered = useMemo(() => {
    if (filter === "ALL") return logs;
    return logs.filter((e) => e.level === filter);
  }, [logs, filter]);

  const counts = useMemo(() => {
    const c: Record<string, number> = { ALL: logs.length };
    for (const e of logs) {
      c[e.level] = (c[e.level] || 0) + 1;
    }
    return c;
  }, [logs]);

  return (
    <div>
      {/* Toolbar */}
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 16, flexWrap: "wrap" }}>
        {["ALL", "ERROR", "WARNING", "INFO"].map((level) => (
          <button
            key={level}
            className={`btn btn-sm ${filter === level ? "btn-primary" : "btn-ghost"}`}
            style={{ fontSize: 11 }}
            onClick={() => setFilter(level)}
          >
            {level} ({counts[level] || 0})
          </button>
        ))}
        <span className="flex-spacer" />
        <label style={{ fontSize: 11, color: "var(--text-muted)", display: "flex", alignItems: "center", gap: 4, cursor: "pointer" }}>
          <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} />
          {t("logs.auto_scroll")}
        </label>
      </div>

      {/* Log panel */}
      <div
        className="log-panel"
        ref={useCallback((el: HTMLDivElement | null) => { if (autoScroll && el) el.scrollTop = el.scrollHeight; }, [autoScroll])}
        style={{ display: "block", maxHeight: "calc(100vh - 220px)", overflowY: "auto" }}
      >
        {filtered.length === 0 ? (
          <div style={{ color: "var(--text-muted)", textAlign: "center", padding: 40 }}>
            {logs.length === 0 ? t("logs.empty") : t("logs.no_match")}
          </div>
        ) : (
          filtered.map((entry, i) => (
            <div key={i} style={{ fontFamily: "var(--font-mono)", fontSize: 11, lineHeight: 1.7 }}>
              <span style={{ color: "var(--text-muted)" }}>[{entry.time}]</span>{" "}
              <span style={{ color: LEVEL_COLORS[entry.level] || "var(--text-muted)" }}>
                {entry.level.padEnd(7)}
              </span>{" "}
              {entry.message}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
