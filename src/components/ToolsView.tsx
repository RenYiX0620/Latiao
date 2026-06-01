import { useTranslation } from "../i18n";

const SIDECAR = "http://127.0.0.1:8000";

interface ToolsViewProps {
  tools: { name: string; description: string; parameters: Record<string, unknown>; permission: string; usage_count: number }[];
  setTools: React.Dispatch<React.SetStateAction<{ name: string; description: string; parameters: Record<string, unknown>; permission: string; usage_count: number }[]>>;
  showToast: (msg: string) => void;
}

export default function ToolsView({ tools, setTools, showToast }: ToolsViewProps) {
  const { t } = useTranslation();
  const iconMap: Record<string, string> = { read_file: "📄", write_file: "✏️", list_dir: "📁", run_cmd: "⚡", open_folder: "📂", open_app: "🚀", search_files: "🔍" };

  return (
    <div className="card-grid">
      {tools.map((tool) => {
        const isSafe = tool.permission === "safe";
        return (
        <div key={tool.name} className="card" style={isSafe ? {} : { borderLeft: "2px solid var(--warning)" }}>
          <div className="card-title">{iconMap[tool.name] || "🔧"} {tool.name}
            <span className={`badge ${isSafe ? "badge-safe" : "badge-confirm"}`}>{isSafe ? t("tools.safe") : t("tools.confirm")}</span>
          </div>
          <div className="card-desc">{tool.description}</div>
          <div className="card-meta" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span>{t("tools.calls", { count: tool.usage_count })}</span>
            <span style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
              {Object.keys(tool.parameters?.properties || {}).length > 0
                ? Object.keys((tool.parameters as Record<string, unknown>)?.properties as Record<string, unknown> || {}).join(", ")
                : t("tools.no_params")}
            </span>
          </div>
          <button className={`btn btn-sm ${isSafe ? "btn-ghost" : "btn-primary"}`} style={{ marginTop: 10, width: "100%" }}
            onClick={async (e) => {
              e.stopPropagation();
              const newPerm = isSafe ? "confirm" : "safe";
              try {
                await fetch(SIDECAR + "/v1/permissions", {
                  method: "POST", headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ tool: tool.name, permission: newPerm }),
                });
                setTools(prev => prev.map(t2 => t2.name === tool.name ? { ...t2, permission: newPerm } : t2));
                showToast(`${tool.name} → ${newPerm === "safe" ? t("tools.safe") : t("tools.confirm")}`);
              } catch (e) { console.error(e); showToast(t("tools.toggle_fail")); }
            }}>
            {isSafe ? t("tools.set_confirm") : t("tools.set_safe")}
          </button>
        </div>
      )})}
    </div>
  );
}
