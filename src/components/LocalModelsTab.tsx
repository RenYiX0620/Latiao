import { lazy, Suspense, useState } from "react";
import { fetch } from "@tauri-apps/plugin-http";
import { open, ask } from "@tauri-apps/plugin-dialog";
import { useTranslation } from "../i18n";
import type { HFModelResult, DownloadState, SetupIssue, LLMStatus } from "../types";

const ReactMarkdown = lazy(() => import("react-markdown"));
const SIDECAR = "http://127.0.0.1:8000";

function ModelDetailPanel({ modelId, detailData, detailLoading, downloadProgress, downloadModel, pauseDownload, cancelDownload, resumeDownload, startLocalLLM, deleteModelFile, fetchModelDetail, onClose }: {
  modelId: string; detailData: Record<string, unknown> | null; detailLoading: boolean;
  downloadProgress: Record<string, DownloadState>; downloadModel: (id: string) => void;
  pauseDownload: (id: string) => void; cancelDownload: (id: string) => void;
  resumeDownload: (id: string) => void;
  startLocalLLM: (id?: string) => void; deleteModelFile: (id: string) => void; fetchModelDetail: (id: string) => void; onClose: () => void;
}) {
  const { t } = useTranslation();
  if (!modelId) return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "#d1d5db", fontSize: 13, padding: 40, textAlign: "center" }}>
      <div><div style={{ fontSize: 40, marginBottom: 12 }}>📦</div>{t("local.select_model_first")}<div style={{ fontSize: 10, marginTop: 4, color: "#e5e7eb" }}>{t("local.view_details")}</div></div>
    </div>
  );
  if (detailLoading) return (
    <div style={{ textAlign: "center", padding: 60 }}>
      <div style={{ width: 24, height: 24, border: "3px solid #e5e7eb", borderTopColor: "#7c3aed", borderRadius: "50%", animation: "spin 0.8s linear infinite", margin: "0 auto 10px" }} />
      <span style={{ color: "#9ca3af", fontSize: 12 }}>{t("local.loading")}</span>
    </div>
  );
  if (!detailData || detailData.status === "error") return (
    <div style={{ textAlign: "center", padding: 40, color: "#ef4444", fontSize: 12 }}>
      {t("local.load_fail")} — <button className="btn btn-sm btn-ghost" onClick={() => fetchModelDetail(modelId)}>{t("local.retry")}</button>
    </div>
  );

  const tags = (detailData.tags as string[]) || [];
  const allLower = tags.map((t: string) => t.toLowerCase());
  const caps: Array<[string, string, string, string]> = [];
  if (allLower.some((t: string) => t.includes("vision") || t.includes("image"))) caps.push(["👁", "Vision", "#92400e", "#fef3c7"]);
  if (allLower.some((t: string) => t.includes("tool") || t.includes("function-calling") || t.includes("agent"))) caps.push(["🔧", "Tool Use", "#1e40af", "#dbeafe"]);
  if (allLower.some((t: string) => t.includes("reasoning") || t.includes("think") || t.includes("r1"))) caps.push(["🧠", "Reasoning", "#065f46", "#d1fae5"]);
  if (allLower.some((t: string) => t.includes("gguf"))) caps.push(["📦", "GGUF", "#3b82f6", "#dbeafe"]);

  const specs: Array<[string, string, boolean?]> = [];
  const seenLabels = new Set<string>();
  const pm = modelId.match(/(\d+)[bB]/);
  if (pm) { specs.push(["Params", pm[1] + "B"]); seenLabels.add("Params"); }
  for (const t of tags) { const l = t.toLowerCase(); if (l.includes("gguf") && !seenLabels.has("Format")) { specs.push(["Format", "GGUF", true]); seenLabels.add("Format"); } else if (!seenLabels.has("Domain") && ["llm","nlp","code","vision"].includes(l)) { specs.push(["Domain", t]); seenLabels.add("Domain"); } else if (!seenLabels.has("Arch") && ["gemma","llama","qwen","mistral","phi","deepseek"].some(a => l.includes(a))) { specs.push(["Arch", t]); seenLabels.add("Arch"); } }

  // Estimate file size from quantization level when HF returns 0 (LFS files)
  const quantOrder: Record<string, number> = {F16:0,Q8_0:1,Q6_K:2,Q6_K_L:3,Q5_K_M:4,Q5_K_L:5,Q5_K_S:6,Q5_0:7,Q4_K_M:8,Q4_K_L:9,Q4_K_S:10,Q4_0:11,Q4_1:12,Q3_K_XL:13,Q3_K_L:14,Q3_K_M:15,Q3_K_S:16,Q2_K_L:17,Q2_K:18,IQ4_NL:19,IQ4_XS:20,IQ3_M:21,IQ3_XS:22,IQ3_XXS:23,IQ2_M:24,IQ2_S:25,IQ1_M:26,IQ1_S:27};
  // Bits-per-weight estimates for common quantization levels (used when HF reports 0 bytes for LFS files)
  const quantBpw: Record<string, number> = {F16:16,Q8_0:8.5,Q6_K:6.6,Q6_K_L:6.6,Q5_K_M:5.5,Q5_K_L:5.5,Q5_K_S:5.5,Q5_0:5.5,Q4_K_M:4.8,Q4_K_L:4.8,Q4_K_S:4.8,Q4_0:4.5,Q4_1:4.5,Q3_K_XL:3.5,Q3_K_L:3.5,Q3_K_M:3.5,Q3_K_S:3.5,Q2_K_L:2.7,Q2_K:2.6,IQ4_NL:4.5,IQ4_XS:4.2,IQ3_M:3.4,IQ3_XS:3.3,IQ3_XXS:3.1,IQ2_M:2.5,IQ2_S:2.4,IQ1_M:1.7,IQ1_S:1.6};
  // Extract parameter count from modelId (e.g. "Qwen2.5-7B-Instruct" → 7)
  const paramMatch = modelId.match(/(\d+)[bB]/);
  const paramB = paramMatch ? parseFloat(paramMatch[1]) : 0;
  const estimateSize = (sizeBytes: number, quant: string): string => {
    if (sizeBytes > 0) return (sizeBytes / (1024**3)).toFixed(1) + " GB";
    if (paramB > 0 && quantBpw[quant]) {
      const estimatedGB = (paramB * quantBpw[quant]) / 8;
      return "≈" + estimatedGB.toFixed(1) + " GB";
    }
    return "";
  };
  // Deduplicate split files (00001-of-00003) into single entries
  const rawFiles = ((detailData.siblings as Array<{filename:string;size:string;quant:string;size_bytes:number}>) || [])
    .filter(s => s.filename.endsWith(".gguf") && !s.filename.includes("mmproj") && !s.filename.includes("/mmproj"));
  const merged = new Map<string, {filename:string;size_bytes:number;quant:string;parts:number}>();
  for (const f of rawFiles) {
    const base = f.filename.replace(/-\d{5}-of-\d{5}/, "").replace(/\.gguf$/, "");
    const existing = merged.get(base);
    if (existing) {
      existing.size_bytes += f.size_bytes;
      existing.parts += 1;
      if (f.filename.length < existing.filename.length) existing.filename = f.filename;
    } else {
      merged.set(base, {filename: f.filename, size_bytes: f.size_bytes, quant: f.quant, parts: 1});
    }
  }
  const ggufFiles = Array.from(merged.values())
    .sort((a,b) => (quantOrder[a.quant] ?? 99) - (quantOrder[b.quant] ?? 99));

  return (
    <>
      <div style={{ padding: "14px 18px", borderBottom: "1px solid #e8eaed", position: "sticky", top: 0, background: "#fafbfc", zIndex: 2 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: "#1a1a2e" }}>{modelId.split("/").pop()?.replace(/-/g, " ").replace(/_/g, " ") || modelId}</div>
            <div style={{ fontSize: 10, color: "#9ca3af", fontFamily: "var(--font-mono)", marginTop: 1 }}>{modelId}</div>
          </div>
          <button onClick={onClose} style={{ width: 28, height: 28, borderRadius: 6, border: "none", background: "transparent", cursor: "pointer", color: "#9ca3af", fontSize: 16 }}>✕</button>
        </div>
      </div>
      <div style={{ padding: "14px 18px" }}>
        <div style={{ display: "flex", gap: 12, marginBottom: 12, fontSize: 11, color: "#6b7280" }}>
          <span>⬇ {(detailData.downloads as number)?.toLocaleString() || 0}</span>
          <span>❤️ {(detailData.likes as number) || 0}</span>
          {((detailData.downloads as number) > 100000 || (detailData.likes as number) > 50) && <span style={{ marginLeft: "auto", padding: "2px 8px", borderRadius: 5, background: "#f5f3ff", color: "#7c3aed", fontSize: 9, fontWeight: 600 }}>✨ Staff Pick</span>}
        </div>
        {caps.length > 0 && (<div style={{ marginBottom: 12 }}><div style={{ fontSize: 10, color: "#9ca3af", marginBottom: 4 }}>Capabilities:</div><div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>{caps.map(([icon, label, color, bg]) => (<span key={label} style={{ padding: "3px 8px", borderRadius: 12, fontSize: 10, fontWeight: 600, background: bg, color, border: `1px solid ${color}20`, display: "flex", alignItems: "center", gap: 3 }}>{icon} {label}</span>))}</div></div>)}
        {specs.length > 0 && (<div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 12px", marginBottom: 14 }}>{specs.slice(0,4).map(([label, value, accent]) => (<div key={label} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11 }}><span style={{ color: "#9ca3af", minWidth: 44 }}>{label}</span><span style={{ padding: "1px 6px", borderRadius: 4, fontSize: 10, fontFamily: "var(--font-mono)", fontWeight: 600, background: accent ? "#dbeafe" : "#f3f4f6", color: accent ? "#2563eb" : "#4b5563" }}>{value}</span></div>))}</div>)}
        {ggufFiles.length > 0 && (<div style={{ marginBottom: 14 }}><div style={{ fontSize: 11, fontWeight: 700, color: "#1f2937", marginBottom: 6 }}>📦 下载选项（{ggufFiles.length} 个版本）</div>{ggufFiles.map(sib => { const fullModelId = modelId + "/" + sib.filename; const dp = downloadProgress[sib.filename] || downloadProgress[fullModelId]; const sizeDisplay = estimateSize(sib.size_bytes, sib.quant); const label = sib.filename.split("/").pop()?.replace(/-\d{5}-of-\d{5}\.gguf$/, ".gguf") || sib.filename; return (<div key={sib.filename} style={{ display: "flex", alignItems: "center", gap: 6, padding: "6px 10px", background: dp?.status==="done"?"#ecfdf5":"#fff", borderRadius: 8, marginBottom: 4, border: `1px solid ${dp?.status==="done"?"#10b981":"#e5e7eb"}` }}><span style={{ fontSize: 9, fontWeight: 700, fontFamily: "var(--font-mono)", padding: "2px 6px", borderRadius: 4, background: "#ede9fe", color: "#7c3aed", minWidth: 52, textAlign: "center" }}>{sib.quant||"Weight"}</span><div style={{ flex: 1, minWidth: 0 }}><div style={{ fontSize: 9, fontFamily: "var(--font-mono)", color: "#4b5563", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{label}</div><div style={{ fontSize: 9, color: "#9ca3af" }}>{sizeDisplay}{sib.parts > 1 ? ` · ${sib.parts}分片` : ""}</div></div>{dp?.status==="done"?<div style={{ display:"flex", alignItems:"center", gap:4, flexShrink:0 }}><button className="btn btn-sm btn-primary" style={{ padding: "4px 10px", fontSize: 10, borderRadius: 6 }} onClick={() => startLocalLLM(fullModelId)}>🚀</button><button className="btn btn-sm" style={{ padding:"2px 6px", fontSize:9, borderRadius:4, background:"transparent", color:"#ef4444", border:"1px solid #fecaca" }} onClick={async (e) => { e.stopPropagation(); const ok = await ask(`确定要删除 ${label} 吗？`); if (ok) deleteModelFile(fullModelId); }} title="删除模型文件">🗑</button></div>:dp?.status==="downloading"?<div style={{ display:"flex", alignItems:"center", gap:4, flexShrink:0 }}><div style={{ width:40, height:4, borderRadius:2, background:"#e5e7eb", overflow:"hidden" }}><div style={{ width:dp?.progress+"%", height:"100%", borderRadius:2, background:"#7c3aed", transition:"width 0.5s" }} /></div><span style={{ fontSize:9, color:"#7c3aed", fontWeight:600, minWidth:24, textAlign:"right" }}>{dp?.progress||0}%</span><span style={{ fontSize:8, color:"#9ca3af", minWidth:44, textAlign:"right" }}>{(dp?.speed_bps||0)>0?((dp?.speed_bps||0)/(1024**2)).toFixed(1)+" MB/s":""}</span><span style={{ fontSize:8, color:"#d1d5db" }}>{(dp?.eta_seconds||0)>0?((dp?.eta_seconds||0)>3600?Math.floor((dp?.eta_seconds||0)/3600)+"h":(dp?.eta_seconds||0)>60?Math.floor((dp?.eta_seconds||0)/60)+"m":(dp?.eta_seconds||0)+"s"):""}</span><button className="btn btn-sm" style={{ padding:"2px 6px", fontSize:9, borderRadius:4, background:"#f59e0b", color:"#fff", border:"none" }} onClick={() => pauseDownload(fullModelId)} title="暂停">⏸</button><button className="btn btn-sm" style={{ padding:"2px 6px", fontSize:9, borderRadius:4, background:"#ef4444", color:"#fff", border:"none" }} onClick={() => cancelDownload(fullModelId)} title="取消">✕</button></div>:dp?.status==="error"||dp?.status==="cancelled"?<div style={{ display:"flex", alignItems:"center", gap:4 }}><span style={{ fontSize:8, color:"#ef4444", maxWidth:100, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }} title={dp?.message||""}>{dp?.status==="cancelled"?t("local.cancelled"):t("local.failed")}</span><button className="btn btn-sm" style={{ padding:"2px 6px", fontSize:9, borderRadius:4, background:"#ef4444", color:"#fff", border:"none" }} onClick={() => cancelDownload(fullModelId)} title="删除记录">🗑</button><button className="btn btn-sm" style={{ padding:"4px 10px", fontSize:10, borderRadius:6, background:"#7c3aed", color:"#fff", border:"none", fontWeight:600 }} onClick={() => downloadModel(fullModelId)}>↻</button></div>:dp?.status==="paused"?<div style={{ display:"flex", alignItems:"center", gap:4 }}><span style={{ fontSize:8, color:"#f59e0b" }}>已暂停</span><button className="btn btn-sm" style={{ padding:"2px 6px", fontSize:9, borderRadius:4, background:"#ef4444", color:"#fff", border:"none" }} onClick={() => cancelDownload(fullModelId)} title="取消">✕</button><button className="btn btn-sm" style={{ padding:"4px 10px", fontSize:10, borderRadius:6, background:"#f59e0b", color:"#fff", border:"none", fontWeight:600 }} onClick={() => resumeDownload(fullModelId)}>▶ 继续</button></div>:<button className="btn btn-sm" style={{ padding: "4px 10px", fontSize: 10, borderRadius: 6, background: "#7c3aed", color: "#fff", border: "none", fontWeight: 600 }} onClick={() => downloadModel(fullModelId)}>⬇</button>}</div>); })}</div>)}
        {detailData.readme ? <details open><summary style={{ fontSize: 11, fontWeight: 700, color: "#1f2937", cursor: "pointer", marginBottom: 6 }}>📖 README</summary><div style={{ fontSize: 10, lineHeight: 1.6, color: "#374151", maxHeight: 300, overflowY:"auto", padding: "10px 12px", background: "#fff", borderRadius: 8, border: "1px solid #e5e7eb" }}><Suspense fallback={<div>{String(detailData.readme).slice(0, 500)}</div>}><ReactMarkdown>{String(detailData.readme).slice(0, 3000)}</ReactMarkdown></Suspense></div></details> : null}
      </div>
    </>
  );
}

interface Props {
  localLLMStatus: LLMStatus; localModelId: string; setLocalModelId: (id: string) => void;
  setupCheck: { ready: boolean; ok: { item: string; status: string }[]; issues: SetupIssue[] } | null;
  hfSearch: string; setHfSearch: (s: string) => void; hfResults: HFModelResult[]; searching: boolean; searchHF: (query?: string, library?: string) => void;
  downloadProgress: Record<string, DownloadState>; downloadModel: (modelId: string) => void;
  pauseDownload: (modelId: string) => void; cancelDownload: (modelId: string) => void;
  resumeDownload: (modelId: string) => void;
  startLocalLLM: (modelId?: string) => void; stopLocalLLM: () => void;
  fixing: string; runFix: (fixType: string, fixPkg: string) => void; showToast: (msg: string, type?: string) => void;
  contextLimit: number; setContextLimit: (limit: number) => void;
  contextEstimate: { max_context: number; recommended_context: number; ram_available_gb: number; memory_for_context_gb: number } | null;
  fetchContextEstimate: (modelPath?: string) => void;
}

export default function LocalModelsTab(props: Props) {
  const { localLLMStatus, localModelId, setLocalModelId, setupCheck, hfSearch, setHfSearch, hfResults, searching, searchHF, downloadProgress, downloadModel, pauseDownload, cancelDownload, resumeDownload, startLocalLLM, stopLocalLLM, fixing, runFix, showToast, contextLimit, setContextLimit, contextEstimate, fetchContextEstimate } = props;
  const { t } = useTranslation();
  const isRunning = localLLMStatus.status === "running";
  const isStarting = localLLMStatus.status === "starting";
  const [showSearch, setShowSearch] = useState(false);
  const [searchFilter, setSearchFilter] = useState("");
  const [detailModelId, setDetailModelId] = useState("");
  const [detailData, setDetailData] = useState<Record<string, unknown> | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const filteredResults = searchFilter ? hfResults.filter(m => m.tags?.some(t => t.toLowerCase().includes(searchFilter))) : hfResults;

  const openSearch = () => { setShowSearch(true); setHfSearch(""); setDetailModelId(""); setDetailData(null); searchHF(""); };
  const fetchModelDetail = async (modelId: string) => { setDetailModelId(modelId); setDetailData(null); setDetailLoading(true); try { const resp = await fetch(SIDECAR + "/v1/local-llm/model-detail?model_id=" + encodeURIComponent(modelId)); setDetailData(await resp.json()); } catch { setDetailData({ status: "error", message: "加载失败" }); } setDetailLoading(false); };
  const deleteModelFile = async (modelId: string) => { try { const resp = await fetch(SIDECAR + "/v1/local-llm/delete-model", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model_id: modelId }) }); const data = await resp.json(); showToast(data.message || t("local.deleted")); if (data.status === "ok" && detailModelId) fetchModelDetail(detailModelId); } catch(e) { console.error(e); showToast(t("local.delete_fail")); } };

  return (
    <div style={{ position: "relative" }}>
      {setupCheck && setupCheck.issues.length > 0 && (
        <div className="settings-group" style={{ marginBottom: 16, borderColor: "var(--warning)" }}>
          <div className="settings-group-header" style={{ color: "var(--warning)" }}>⚠ {t("local.env_check")}</div>
          {setupCheck.issues.map((iss, i) => (
            <div key={i} className="settings-row" style={{ background: iss.status === "missing" ? "var(--warning-soft)" : "transparent" }}>
              <div style={{ flex: 1 }}><div className="settings-row-label">{iss.item}</div><div className="settings-row-desc" style={{ fontFamily: "var(--font-mono)", fontSize: 10 }}>{iss.fix}</div></div>
              {iss.fix_type === "pip" ? <button className="btn btn-sm btn-primary" style={{ flexShrink: 0 }} onClick={() => runFix(iss.fix_type || "", iss.fix_pkg || "")} disabled={fixing === (iss.fix_pkg || "")}>{fixing === (iss.fix_pkg || "") ? t("local.fixing") : t("local.fix_btn")}</button>
              : <button className="btn btn-sm btn-ghost" style={{ flexShrink: 0 }} onClick={() => showToast(t("local.manual_fix") + ": " + iss.fix)}>{t("local.manual_fix")}</button>}
            </div>
          ))}
        </div>
      )}

      <div className="settings-group" style={{ marginBottom: 16 }}>
        <div className="settings-group-header">{t("local.engine_title")}<span style={{ marginLeft: 8, fontSize: 10, fontWeight: 400, color: "var(--text-muted)" }}>{localLLMStatus.backend || t("local.engine_detecting")}{localLLMStatus.platform && <> · {localLLMStatus.platform}</>}</span></div>
        <div style={{ padding: "14px 16px", display: "flex", alignItems: "center", gap: 16 }}>
          <div style={{ width: 52, height: 52, borderRadius: "50%", background: isRunning ? "linear-gradient(135deg, var(--success), #10b981)" : isStarting ? "linear-gradient(135deg, var(--warning), #f59e0b)" : "linear-gradient(135deg, var(--border-default), var(--text-muted))", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 22, flexShrink: 0, boxShadow: isRunning ? "0 0 20px rgba(52,211,153,0.3)" : "none", transition: "all 0.5s ease" }}>{isRunning ? "⚡" : isStarting ? "⏳" : "⏹"}</div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 15, fontWeight: 600, color: isRunning ? "var(--success)" : isStarting ? "var(--warning)" : "var(--text-muted)" }}>{isRunning ? t("local.status_running") : isStarting ? t("local.status_starting") : t("local.status_stopped")}</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>{localLLMStatus.message || t("local.ready")}</div>
            {isRunning && <>
              <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 6, fontFamily: "var(--font-mono)", display: "flex", gap: 16, flexWrap: "wrap" }}><span>🖥 {localLLMStatus.model_name || localLLMStatus.model_id}</span><span>🔌 :{localLLMStatus.port}</span><span>📐 {localLLMStatus.token_limit.toLocaleString()} tokens</span>{localLLMStatus.gpu_layers !== undefined && <span>🧮 {localLLMStatus.gpu_layers === -1 ? "Auto GPU" : `${localLLMStatus.gpu_layers} layers`}</span>}</div>
              <div style={{ marginTop: 8, display: "flex", gap: 8 }}><button className="btn btn-sm btn-primary" onClick={stopLocalLLM} style={{ padding: "6px 16px" }}>⏹ {t("local.stop_btn")}</button><button className="btn btn-sm btn-ghost" style={{ padding: "6px 16px", color: "var(--danger)" }} onClick={async () => { const mid = localLLMStatus.model_id; if (!mid) return; const ok = await ask(t("local.delete_model_confirm") + "\n" + mid); if (!ok) return; try { await stopLocalLLM(); const resp = await fetch(SIDECAR + "/v1/local-llm/delete-model", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model_id: mid }) }); const data = await resp.json(); showToast(data.message || t("local.deleted")); } catch(e) { console.error(e); showToast(t("local.delete_fail")); } }}>{t("local.delete_model_btn")}</button></div>
            </>}
          </div>
        </div>
      </div>

      <div className="settings-group" style={{ marginBottom: 16 }}>
        <div className="settings-group-header">📐 上下文长度{contextEstimate && <span style={{ marginLeft: 8, fontSize: 10, fontWeight: 400, color: "var(--text-muted)" }}>可用内存 {contextEstimate.ram_available_gb}GB · 推荐 {contextEstimate.recommended_context.toLocaleString()} tokens</span>}</div>
        <div style={{ padding: "12px 16px" }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <input type="range" min={2048} max={131072} step={2048} value={contextLimit} onChange={e => setContextLimit(parseInt(e.target.value))} style={{ flex: 1, minWidth: 200 }} />
            <select className="form-input" style={{ width: 120, margin: 0, fontSize: 12, padding: "6px 8px", fontFamily: "var(--font-mono)" }} value={contextLimit} onChange={e => setContextLimit(parseInt(e.target.value))}>
              {[2048, 4096, 8192, 16384, 32768, 49152, 65536, 98304, 131072].map(v => <option key={v} value={v}>{v.toLocaleString()}</option>)}
            </select>
            <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--accent)", minWidth: 80, textAlign: "right" }}>{contextLimit.toLocaleString()} tokens</span>
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 8, fontSize: 10, color: "var(--text-muted)", alignItems: "center" }}>
            <button className="btn btn-sm btn-ghost" style={{ fontSize: 10, padding: "2px 8px" }} onClick={() => fetchContextEstimate(localModelId || undefined)}>{t("local.redetect")}</button>
            <button className="btn btn-sm btn-ghost" style={{ fontSize: 10, padding: "2px 8px" }} onClick={() => startLocalLLM()} disabled={!localLLMStatus.model_id}>{t("local.reload_model")}</button>
            {contextEstimate && <><span>| {t("local.max_safe")}: {contextEstimate.max_context.toLocaleString()} tokens</span><span>| {t("local.restart_effect")}</span></>}
          </div>
          {contextEstimate && contextLimit > contextEstimate.max_context && <div style={{ marginTop: 8, fontSize: 10, color: "var(--danger)" }}>{t("local.context_warning", { limit: contextEstimate.max_context.toLocaleString() })}</div>}
        </div>
      </div>

      <div className="settings-group" style={{ marginBottom: 16 }}>
        <div className="settings-group-header">{t("local.hf_search")}</div>
        <div style={{ padding: "12px 16px" }}>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn btn-md btn-primary" onClick={openSearch} style={{ flex: 1, padding: "10px 16px", fontSize: 13 }}>🔍 {t("local.search_btn")}</button>
          </div>
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 6 }}>{t("local.browse_hint")}</div>
        </div>
      </div>

      {!isRunning && (
        <div className="settings-group" style={{ marginBottom: 16 }}>
          <div className="settings-group-header">{t("local.start_model")}</div>
          <div style={{ padding: "12px 16px" }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <input className="form-input" style={{ flex: 1, margin: 0, fontSize: 12, padding: "8px 12px", fontFamily: "var(--font-mono)" }} placeholder={t("local.model_id_placeholder")} value={localModelId} onChange={e => setLocalModelId(e.target.value)} onKeyDown={e => { if (e.key === "Enter") startLocalLLM(); }} />
              <button className="btn btn-sm btn-primary" onClick={async () => { try { const selected = await open({ multiple: false, filters: [{ name: "Model Files", extensions: ["gguf", "mlx"] }] }); if (selected) { setLocalModelId(selected); startLocalLLM(selected); } } catch { /* dialog cancelled */ } }} title={t("local.select_file")} style={{ minWidth: 100, padding: "8px 16px" }}>📁 {t("local.select_file")}</button>
              <button className="btn btn-md btn-primary" style={{ minWidth: 100, padding: "8px 16px" }} onClick={() => startLocalLLM()} disabled={isStarting}>{isStarting ? "⏳ " + t("local.starting") : "🚀 " + t("local.start")}</button>
            </div>
            <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 8 }}>{t("local.start_hint")} {localLLMStatus.backend === "mlx" ? t("local.mlx") : t("local.llamacpp")}</div>
          </div>
        </div>
      )}

      {showSearch && (
        <>
          <div onClick={() => setShowSearch(false)} style={{ position: "absolute", inset: 0, background: "rgba(0,0,0,0.3)", backdropFilter: "blur(2px)", WebkitBackdropFilter: "blur(2px)", zIndex: 199 }} />
          <div style={{ position: "absolute", top: 16, left: 16, right: 16, bottom: 16, background: "var(--bg-card)", borderRadius: 14, boxShadow: "0 20px 60px rgba(0,0,0,0.25)", zIndex: 200, display: "flex", overflow: "hidden" }}>
            <div style={{ width: 340, minWidth: 280, flexShrink: 0, borderRight: "1px solid var(--border-default)", display: "flex", flexDirection: "column", overflow: "hidden" }}>
              <div style={{ padding: "14px 16px", borderBottom: "1px solid var(--border-default)", display: "flex", gap: 8 }}>
                <input className="form-input" style={{ flex: 1, margin: 0, fontSize: 13, padding: "8px 12px" }} placeholder={t("local.search_placeholder")} value={hfSearch} onChange={e => setHfSearch(e.target.value)} onKeyDown={e => { if (e.key === "Enter") searchHF(); }} autoFocus />
                <button className="btn btn-sm btn-primary" onClick={() => searchHF()} disabled={searching} style={{ padding: "8px 14px", fontSize: 12, whiteSpace: "nowrap" }}>{searching ? "..." : "🔍"}</button>
                <button className="btn btn-sm btn-ghost" onClick={() => { setShowSearch(false); setDetailModelId(""); }} style={{ fontSize: 18, padding: "4px 8px" }}>✕</button>
              </div>
              <div style={{ padding: "6px 12px", display: "flex", gap: 6, borderBottom: "1px solid #f0f0f0", flexWrap: "wrap" }}>
                {[{ label: "全部", value: "" }, { label: "GGUF", value: "gguf" }, { label: "MLX", value: "mlx" }].map(f => (
                  <button key={f.label} onClick={() => { setSearchFilter(f.value); if (f.value) searchHF(hfSearch || f.value, f.value); else searchHF(hfSearch); }} style={{ padding: "4px 10px", borderRadius: 14, fontSize: 10, fontWeight: 500, cursor: "pointer", border: `1px solid ${searchFilter === f.value ? "#7c3aed" : "#e5e7eb"}`, background: searchFilter === f.value ? "#f5f3ff" : "#fff", color: searchFilter === f.value ? "#7c3aed" : "#6b7280" }}>{f.label}</button>
                ))}
              </div>
              <div className="custom-scrollbar" style={{ flex: 1, overflowY: "auto", padding: "8px 12px" }}>
                {searching && <div style={{ textAlign: "center", padding: 40, color: "#9ca3af", fontSize: 12 }}>{t("local.searching")}</div>}
                {!searching && filteredResults.length === 0 && <div style={{ textAlign: "center", padding: 40, color: "#9ca3af", fontSize: 12 }}>{hfSearch ? t("local.no_results") : t("local.trending")}</div>}
                {filteredResults.map((m: HFModelResult) => (
                  <div key={m.id} onClick={() => fetchModelDetail(m.id)} style={{ padding: "10px 12px", borderRadius: 8, marginBottom: 4, cursor: "pointer", background: detailModelId === m.id ? "#f5f3ff" : "transparent", border: detailModelId === m.id ? "1px solid #ddd6fe" : "1px solid transparent", transition: "all 0.15s" }}
                    onMouseEnter={e => { if (detailModelId !== m.id) (e.currentTarget as HTMLElement).style.background = "#f9fafb"; }} onMouseLeave={e => { if (detailModelId !== m.id) (e.currentTarget as HTMLElement).style.background = "transparent"; }}>
                    <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
                      <div style={{ width: 28, height: 28, borderRadius: 6, background: "linear-gradient(135deg, #667eea, #764ba2)", display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontSize: 12, flexShrink: 0 }}>🧩</div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 12, fontWeight: 600, color: "#1f2937", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{m.id.split("/").pop()?.replace(/-/g, " ").replace(/_/g, " ") || m.id}</div>
                        <div style={{ fontSize: 10, color: "#9ca3af", fontFamily: "var(--font-mono)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{m.id.split("/")[0]}</div>
                        <div style={{ fontSize: 9, color: "#d1d5db", marginTop: 2 }}>⬇ {m.downloads?.toLocaleString() || 0} · ❤️ {m.likes || 0}</div>
                      </div>
                      {downloadProgress[m.id]?.status === "downloading" && <span style={{ fontSize: 9, color: "#7c3aed", fontWeight: 600 }}>{downloadProgress[m.id].progress}%</span>}
                      {downloadProgress[m.id]?.status === "done" && <span style={{ fontSize: 12 }}>✅</span>}
                    </div>
                  </div>
                ))}
              </div>
            </div>
            <div className="custom-scrollbar" style={{ flex: 1, overflowY: "auto", background: "#fafbfc" }}>
              <ModelDetailPanel modelId={detailModelId} detailData={detailData} detailLoading={detailLoading} downloadProgress={downloadProgress} downloadModel={downloadModel} pauseDownload={pauseDownload} cancelDownload={cancelDownload} resumeDownload={resumeDownload} startLocalLLM={startLocalLLM} deleteModelFile={deleteModelFile} fetchModelDetail={fetchModelDetail} onClose={() => setDetailModelId("")} />
            </div>
          </div>
        </>
      )}
    </div>
  );
}
