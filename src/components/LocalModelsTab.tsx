import { fetch } from "@tauri-apps/plugin-http";
import { useTranslation } from "../i18n";
import type { HFModelResult, DownloadState, SetupIssue, LLMStatus } from "../types";

const SIDECAR = "http://127.0.0.1:8000";

function formatSpeed(bps: number): string {
  if (bps > 1024**3) return (bps / 1024**3).toFixed(1) + " GB/s";
  if (bps > 1024**2) return (bps / 1024**2).toFixed(1) + " MB/s";
  if (bps > 1024) return (bps / 1024).toFixed(0) + " KB/s";
  return bps + " B/s";
}
function formatSize(bytes: number): string {
  if (bytes > 1024**3) return (bytes / 1024**3).toFixed(1) + " GB";
  if (bytes > 1024**2) return (bytes / 1024**2).toFixed(1) + " MB";
  return (bytes / 1024).toFixed(0) + " KB";
}
function formatETA(sec: number): string {
  if (sec > 3600) return Math.ceil(sec / 3600) + "h " + Math.ceil((sec % 3600) / 60) + "m";
  if (sec > 60) return Math.ceil(sec / 60) + "m " + (sec % 60) + "s";
  return sec + "s";
}

interface Props {
  localLLMStatus: LLMStatus;
  localModelId: string;
  setLocalModelId: (id: string) => void;
  setupCheck: { ready: boolean; ok: { item: string; status: string }[]; issues: SetupIssue[] } | null;
  hfSearch: string;
  setHfSearch: (s: string) => void;
  hfResults: HFModelResult[];
  searching: boolean;
  searchHF: (query?: string) => void;
  downloadProgress: Record<string, DownloadState>;
  downloadModel: (modelId: string) => void;
  pauseDownload: (modelId: string) => void;
  resumeDownload: (modelId: string) => void;
  cancelDownload: (modelId: string) => void;
  startLocalLLM: (modelId?: string) => void;
  stopLocalLLM: () => void;
  fixing: string;
  runFix: (fixType: string, fixPkg: string) => void;
  showToast: (msg: string, type?: string) => void;
}

function ModelCard({ m, dp, downloadModel, startLocalLLM, pauseDownload, resumeDownload, cancelDownload, t }
  : { m: HFModelResult; dp?: DownloadState; t: (key: string, params?: Record<string, string | number>) => string; } & Pick<Props, 'downloadModel' | 'startLocalLLM' | 'pauseDownload' | 'resumeDownload' | 'cancelDownload'>) {

  const status = dp?.status;
  const progress = dp?.progress || 0;
  const isDownloading = status === "downloading";
  const isDone = status === "done";
  const isPaused = status === "paused";
  const isError = status === "error";

  return (
    <div className="model-card" style={{
      background: "var(--bg-card)", border: `1px solid ${isDone ? "var(--success)" : isDownloading ? "var(--border-accent)" : "var(--border-default)"}`,
      borderRadius: "var(--radius-lg)", padding: "14px 16px", transition: "border-color 0.3s ease",
    }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
        <div style={{
          width: 40, height: 40, borderRadius: "var(--radius-md)",
          background: "linear-gradient(135deg, var(--accent-soft), transparent)",
          display: "flex", alignItems: "center", justifyContent: "center",
          fontSize: 18, flexShrink: 0,
        }}>
          {isDone ? "✅" : isDownloading ? "📥" : isPaused ? "⏸" : isError ? "⚠️" : "🧩"}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 12, fontWeight: 600, fontFamily: "var(--font-mono)", wordBreak: "break-all" }}>
            {m.id.split("/").pop()}
          </div>
          <div style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginTop: 2 }}>
            {m.id.split("/")[0]}/{m.id.split("/").slice(1).join("/")}
          </div>
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4, display: "flex", gap: 12, flexWrap: "wrap" }}>
            <span>⬇ {m.downloads?.toLocaleString() || 0}</span>
            <span>❤️ {m.likes || 0}</span>
            {m.pipeline_tag && <span style={{ padding: "1px 6px", borderRadius: "var(--radius-sm)", background: "var(--accent-soft)", color: "var(--accent)", fontSize: 9 }}>{m.pipeline_tag}</span>}
            {m.tags?.slice(0, 2).map((tag: string) => (
              <span key={tag} style={{ color: "var(--text-muted)", fontSize: 9 }}>#{tag}</span>
            ))}
          </div>
        </div>
      </div>

      {/* Download progress bar */}
      {isDownloading && (
        <div style={{ marginTop: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4, fontSize: 10, color: "var(--text-muted)" }}>
            <span>{progress}%</span>
            <span>
              {(dp?.speed_bps ?? 0) > 0 && <>{formatSpeed(dp!.speed_bps!)} · </>}
              {(dp?.eta_seconds ?? 0) > 0 && <>{t("local.remaining", { eta: formatETA(dp!.eta_seconds!) })}</>}
              {(dp?.downloaded_bytes ?? 0) > 0 && <> · {formatSize(dp!.downloaded_bytes!)}</>}
            </span>
          </div>
          <div style={{ height: 6, background: "var(--border-default)", borderRadius: 3, overflow: "hidden" }}>
            <div style={{
              height: "100%", width: `${progress}%`,
              background: "linear-gradient(90deg, var(--accent), #6366f1)",
              borderRadius: 3, transition: "width 0.5s ease",
            }}></div>
          </div>
          {dp?.message && (
            <div style={{ fontSize: 9, color: "var(--text-muted)", marginTop: 3, fontFamily: "var(--font-mono)" }}>{dp.message.slice(0, 80)}</div>
          )}
        </div>
      )}

      {isPaused && (
        <div style={{ marginTop: 10, fontSize: 11, color: "var(--warning)", display: "flex", alignItems: "center", gap: 8 }}>
          <span>⏸ {t("local.paused", { pct: progress })}</span>
          {dp?.message && <span style={{ fontSize: 9, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{dp.message.slice(0, 60)}</span>}
        </div>
      )}

      {isError && (
        <div style={{ marginTop: 8, fontSize: 10, color: "var(--danger)" }}>{dp?.message || t("local.download_fail")}</div>
      )}

      {/* Actions */}
      <div style={{ marginTop: 12, display: "flex", gap: 6 }}>
        {isDone ? (
          <>
            <button className="btn btn-sm btn-ghost" style={{ flexShrink: 0 }}
              onClick={async () => {
                await fetch(SIDECAR + "/v1/local-llm/open-path", {
                  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: dp!.path }),
                });
              }}>📂 {t("local.open_folder")}</button>
            <button className="btn btn-sm btn-primary" style={{ flex: 1 }}
              onClick={() => startLocalLLM(m.id)}>🚀 {t("local.start")}</button>
          </>
        ) : isDownloading ? (
          <button className="btn btn-sm btn-ghost" style={{ flex: 1 }}
            onClick={() => pauseDownload(m.id)}>⏸ {t("local.pause")}</button>
        ) : isPaused ? (
          <>
            <button className="btn btn-sm btn-primary" style={{ flex: 1 }}
              onClick={() => resumeDownload(m.id)}>▶ {t("local.resume")}</button>
            <button className="btn btn-sm btn-ghost" style={{ flexShrink: 0 }}
              onClick={() => cancelDownload(m.id)}>{t("local.cancel")}</button>
          </>
        ) : isError ? (
          <button className="btn btn-sm btn-ghost" style={{ flex: 1 }}
            onClick={() => cancelDownload(m.id)}>{t("local.clear_dl")}</button>
        ) : (
          <button className="btn btn-sm btn-primary" style={{ flex: 1 }}
            onClick={() => downloadModel(m.id)}>⬇ {t("local.download")}</button>
        )}
      </div>
    </div>
  );
}

export default function LocalModelsTab({
  localLLMStatus, localModelId, setLocalModelId,
  setupCheck, hfSearch, setHfSearch, hfResults, searching, searchHF,
  downloadProgress, downloadModel, pauseDownload, resumeDownload, cancelDownload,
  startLocalLLM, stopLocalLLM, fixing, runFix, showToast,
}: Props) {
  const { t } = useTranslation();
  const isRunning = localLLMStatus.status === "running";
  const isStarting = localLLMStatus.status === "starting";

  return (
    <div>
      {/* Environment Check */}
      {setupCheck && setupCheck.issues.length > 0 && (
        <div className="settings-group" style={{ marginBottom: 16, borderColor: "var(--warning)" }}>
          <div className="settings-group-header" style={{ color: "var(--warning)" }}>⚠ {t("local.env_check")}</div>
          {setupCheck.issues.map((iss, i) => (
            <div key={i} className="settings-row" style={{ background: iss.status === "missing" ? "var(--warning-soft)" : "transparent" }}>
              <div style={{ flex: 1 }}>
                <div className="settings-row-label">{iss.item}</div>
                <div className="settings-row-desc" style={{ fontFamily: "var(--font-mono)", fontSize: 10 }}>{iss.fix}</div>
              </div>
              {iss.fix_type === "pip" ? (
                <button className="btn btn-sm btn-primary" style={{ flexShrink: 0 }}
                  onClick={() => runFix(iss.fix_type || "", iss.fix_pkg || "")}
                  disabled={fixing === (iss.fix_pkg || "")}>
                  {fixing === (iss.fix_pkg || "") ? t("local.fixing") : t("local.fix_btn")}
                </button>
              ) : (
                <button className="btn btn-sm btn-ghost" style={{ flexShrink: 0 }}
                  onClick={() => showToast(t("local.manual_fix") + ": " + iss.fix)}>{t("local.manual_fix")}</button>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Engine Status Card */}
      <div className="settings-group" style={{ marginBottom: 16 }}>
        <div className="settings-group-header">
          {t("local.engine_title")}
          <span style={{ marginLeft: 8, fontSize: 10, fontWeight: 400, color: "var(--text-muted)" }}>
            {localLLMStatus.backend || t("local.engine_detecting")}
            {localLLMStatus.platform && <> · {localLLMStatus.platform}</>}
          </span>
        </div>

        {/* Status Indicator */}
        <div style={{ padding: "14px 16px", display: "flex", alignItems: "center", gap: 16 }}>
          <div style={{
            width: 52, height: 52, borderRadius: "50%",
            background: isRunning ? "linear-gradient(135deg, var(--success), #10b981)" :
                        isStarting ? "linear-gradient(135deg, var(--warning), #f59e0b)" :
                        "linear-gradient(135deg, var(--border-default), var(--text-muted))",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 22, flexShrink: 0,
            boxShadow: isRunning ? "0 0 20px rgba(52,211,153,0.3)" : "none",
            transition: "all 0.5s ease",
          }}>
            {isRunning ? "⚡" : isStarting ? "⏳" : "⏹"}
          </div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 15, fontWeight: 600, color: isRunning ? "var(--success)" : isStarting ? "var(--warning)" : "var(--text-muted)" }}>
              {isRunning ? t("local.status_running") : isStarting ? t("local.status_starting") : t("local.status_stopped")}
            </div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
              {localLLMStatus.message || t("local.ready")}
            </div>
            {isRunning && (
              <>
                <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 6, fontFamily: "var(--font-mono)", display: "flex", gap: 16, flexWrap: "wrap" }}>
                  <span>🖥 {localLLMStatus.model_name || localLLMStatus.model_id}</span>
                  <span>🔌 :{localLLMStatus.port}</span>
                  <span>📐 {localLLMStatus.token_limit.toLocaleString()} tokens</span>
                  {localLLMStatus.gpu_layers !== undefined && <span>🧮 {localLLMStatus.gpu_layers === -1 ? "Auto GPU" : `${localLLMStatus.gpu_layers} layers`}</span>}
                </div>
                <div style={{ marginTop: 8 }}>
                  <button className="btn btn-sm btn-primary" onClick={stopLocalLLM} style={{ padding: "6px 16px" }}>⏹ {t("local.stop_btn")}</button>
                </div>
              </>
            )}
          </div>
        </div>

        {/* Running info */}
        {isRunning && (
          <div className="settings-row" style={{ borderTop: "1px solid var(--border-default)" }}>
            <div style={{ display: "flex", gap: 12, alignItems: "center", width: "100%", flexWrap: "wrap", fontSize: 11 }}>
              <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
                {localLLMStatus.has_image_support ? "🖼" : "📝"}
                {localLLMStatus.has_image_support ? t("local.img_yes") : t("local.img_no")}
              </span>
              <span style={{ color: "var(--text-muted)" }}>|</span>
              <span>📊 {localLLMStatus.token_limit.toLocaleString()} ctx</span>
              <span style={{ color: "var(--text-muted)" }}>|</span>
              <span>🧠 {localLLMStatus.backend}</span>
              {localLLMStatus.available_backends && (
                <>
                  <span style={{ color: "var(--text-muted)" }}>|</span>
                  <span style={{ color: "var(--text-muted)", fontSize: 10 }}>{t("local.available_backends")}: {localLLMStatus.available_backends.join(", ")}</span>
                </>
              )}
            </div>
          </div>
        )}
      </div>

      {/* HuggingFace Search */}
      <div className="settings-group" style={{ marginBottom: 16 }}>
        <div className="settings-group-header">{t("local.hf_search")}</div>
        <div style={{ padding: "12px 16px" }}>
          <div style={{ display: "flex", gap: 8 }}>
            <input className="form-input" style={{ flex: 1, margin: 0, fontSize: 12, padding: "8px 12px" }}
              placeholder={t("local.search_placeholder")}
              value={hfSearch} onChange={e => setHfSearch(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter") searchHF(); }} />
            <button className="btn btn-md btn-primary" onClick={() => searchHF()} disabled={searching} style={{ padding: "8px 16px" }}>
              {searching ? "..." : "🔍 " + t("local.search_btn")}
            </button>
          </div>

          {/* Search Results */}
          {hfResults.length > 0 && (
            <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 10, maxHeight: 500, overflowY: "auto" }}>
              {hfResults.map((m) => (
                <ModelCard key={m.id} m={m} dp={downloadProgress[m.id]} t={t}
                  downloadModel={downloadModel} startLocalLLM={startLocalLLM}
                  pauseDownload={pauseDownload} resumeDownload={resumeDownload}
                  cancelDownload={cancelDownload} />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Manual Model Input */}
      {!isRunning && (
        <div className="settings-group" style={{ marginBottom: 16 }}>
          <div className="settings-group-header">{t("local.start_model")}</div>
          <div style={{ padding: "12px 16px" }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input className="form-input" style={{ flex: 1, margin: 0, fontSize: 12, padding: "8px 12px", fontFamily: "var(--font-mono)" }}
                placeholder={t("local.model_id_placeholder")}
                value={localModelId} onChange={e => setLocalModelId(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter") startLocalLLM(); }} />
              <button className="btn btn-sm btn-ghost" onClick={async () => {
                try {
                  const resp = await fetch(SIDECAR + "/v1/local-llm/open-path", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: "" }) });
                  if (resp.ok) {
                    const data = await resp.json();
                    if (data.status === "ok") showToast(t("local.folder_opened"));
                  }
                } catch { /* ignore */ }
                // Trigger native file dialog via sidecar
                try {
                  await fetch(SIDECAR + "/v1/local-llm/models");
                } catch { /* ignore */ }
              }} title={t("local.open_folder")}>📂</button>
              <button className="btn btn-md btn-primary" style={{ minWidth: 100, padding: "8px 16px" }} onClick={() => startLocalLLM()}
                disabled={isStarting}>
                {isStarting ? "⏳ " + t("local.starting") : "🚀 " + t("local.start")}
              </button>
            </div>
            <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 8 }}>
              {t("local.start_hint")}
              {" "}{localLLMStatus.backend === "mlx" ? t("local.mlx") : t("local.llamacpp")}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
