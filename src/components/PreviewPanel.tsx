/**
 * 右侧交付物预览栏（2026-09-24）：图 / PDF / 代码内嵌预览，Office 走系统打开。
 * 数据来自 write_file 的 content（代码）或 /v1/file/preview（图/PDF）。
 */
import { memo, useEffect, useState } from "react";
import { X, ExternalLink, FileCode, FileText, Image as ImageIcon, FileWarning } from "lucide-react";
import { useTranslation } from "../i18n";

export type PreviewKind = "image" | "pdf" | "code" | "office" | "unknown";

export interface PreviewItem {
  name: string;
  path: string;
  /** write_file 已带回的正文：代码可直接预览，不必再读盘 */
  content?: string;
}

function kindOf(name: string): PreviewKind {
  const n = name.toLowerCase();
  if (/\.(png|jpe?g|gif|webp|svg)$/.test(n)) return "image";
  if (n.endsWith(".pdf") && !n.endsWith(".pdf.py")) return "pdf";
  if (/\.(docx?|xlsx?|pptx?|pages|numbers|key)$/.test(n) && !/\.(docx?|xlsx?|pptx?)\.\w+$/.test(n)) return "office";
  if (/\.(txt|md|json|js|ts|tsx|jsx|py|rs|go|java|c|cpp|h|hpp|cs|rb|php|swift|kt|html|css|scss|yml|yaml|toml|ini|sh|sql|vue|svelte|xml|csv)$/.test(n)) return "code";
  return "unknown";
}

/** `报告.docx.py` 这类生成器脚本 → 猜同目录真实文档 `报告.docx` */
export function guessDeliverablePath(path: string): string {
  return path.replace(/\.(docx?|xlsx?|pptx?|pdf)\.py$/i, ".$1");
}

/** 服务端 preview 响应 → 前端渲染模式（纯函数，可单测）。
 *
 * 2026-10-10 用户实测「点审阅不显示」：文本类交付物（md/py/json/csv…）服务端返回
 * `kind:"code"` + 正文、**没有 data_base64**——旧代码把它当 image/pdf 处理：
 * `atob("")` 建出一个空 blob 并 `setMode("pdf")`，同时代码渲染分支要求 `!blobUrl`
 * 被空 blob 挡死 → 整个预览栏一片空白。修法：先在此判定，`code` 走文本渲染。 */
export function previewModeFor(
  data: { status?: string; kind?: string; need_soffice?: boolean; message?: string } | null,
): "code" | "image" | "pdf" | "soffice" | "error" {
  if (!data || data.status !== "ok") return "error";
  if (data.need_soffice) return "soffice";
  const k = String(data.kind || "");
  if (k === "code") return "code";
  if (k === "image") return "image";
  return "pdf";
}

async function openInSystem(path: string): Promise<void> {
  const { invoke } = await import("@tauri-apps/api/core");
  try {
    await invoke("open_path", { path });
  } catch {
    try {
      const { openUrl } = await import("@tauri-apps/plugin-opener");
      await openUrl("file://" + path);
    } catch (e) {
      console.warn("open_path failed", e);
    }
  }
}

const PreviewPanel = memo(function PreviewPanel({ item, onClose }: {
  item: PreviewItem | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [err, setErr] = useState("");
  const [needSoffice, setNeedSoffice] = useState(false);
  const [loadingOffice, setLoadingOffice] = useState(false);
  const [mode, setMode] = useState<"idle" | "code" | "pdf" | "image">("idle");
  const [serverText, setServerText] = useState("");
  const [shownName, setShownName] = useState("");

  const kind = item ? kindOf(item.name || item.path) : "unknown";

  useEffect(() => {
    setBlobUrl(null);
    setErr("");
    setNeedSoffice(false);
    setServerText("");
    setMode("idle");
    if (!item) return;
    setShownName(item.name || item.path || "");
    const path = item.path || "";
    const isGenerator = /\.(docx?|xlsx?|pptx?|pdf)\.py$/i.test(path);
    const isOffice = kindOf(item.name || path) === "office" || isGenerator;
    setLoadingOffice(isOffice);
    let revoked: string | null = null;
    let alive = true;
    (async () => {
      // 一律先问服务端：它会把生成器脚本解析到同目录真 Word/Excel
      // （2026-09-24：`生成A股分析报告.py` 旁的 `A股分析报告.docx` 曾被前端短路成代码）
      try {
        const { sidecarFetch } = await import("../utils/api");
        // `x.docx.py` 是生成器：预览同目录 `x.docx`（服务端按文件头嗅探并转 PDF）
        const target = isGenerator ? guessDeliverablePath(path) : path;
        const data = await sidecarFetch(`/v1/file/preview?path=${encodeURIComponent(target)}`);
        if (!alive) return;
        const pmode = previewModeFor(data);
        if (pmode === "error") {
          // 找不到真文档 → 回退显示脚本源码
          if (item.content != null) {
            setMode("code");
            setNeedSoffice(false);
            return;
          }
          setErr(String(data?.message || t("cards.preview_fail")));
          return;
        }
        if (pmode === "soffice") {
          setNeedSoffice(true);
          return;
        }
        if (pmode === "code") {
          // 文本类：服务端只回正文（无 blob）——直接走代码渲染；写入内容缺失时
          // （如从历史恢复的记录）用服务端回读的正文兜底
          setServerText(String((data as { text?: string })?.text || ""));
          setMode("code");
          setNeedSoffice(false);
          return;
        }
        const mime = String(data.mime || "application/octet-stream");
        const bin = atob(String(data.data_base64 || ""));
        const arr = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
        const url = URL.createObjectURL(new Blob([arr], { type: mime }));
        revoked = url;
        setBlobUrl(url);
        setMode(pmode);
        if (target !== path) {
          setShownName(target.split(/[\\/]/).pop() || item.name);
        }
      } catch (e) {
        if (alive) setErr(String(e));
      } finally {
        if (alive) setLoadingOffice(false);
      }
    })();
    return () => {
      alive = false;
      if (revoked) URL.revokeObjectURL(revoked);
    };
  }, [item, kind, t]);

  if (!item) return null;

  return (
    <aside className="preview-panel" aria-label={t("cards.preview")}>
      <div className="preview-panel-head">
        <span className="preview-panel-name" title={item.path}>{shownName || item.name || item.path}</span>
        <div style={{ display: "flex", gap: 6, marginLeft: "auto", flexShrink: 0 }}>
          <button className="btn btn-sm btn-ghost" onClick={() => void openInSystem(item.path)}>
            <ExternalLink size={13} /> {t("cards.open_system")}
          </button>
          <button className="btn-icon" onClick={onClose} title={t("tool.collapse")}>
            <X size={15} />
          </button>
        </div>
      </div>
      <div className="preview-panel-body">
        {err && (
          <div className="preview-empty">
            <FileWarning size={28} />
            <p>{err}</p>
            <button className="btn btn-sm btn-primary" onClick={() => void openInSystem(item.path)}>
              {t("cards.open_system")}
            </button>
          </div>
        )}
        {!err && kind === "office" && loadingOffice && (
          <div className="preview-empty">
            <ImageIcon size={28} className="preview-spin" />
            <p>{t("cards.office_converting")}</p>
          </div>
        )}
        {!err && kind === "office" && !loadingOffice && needSoffice && (
          <div className="preview-empty">
            <FileText size={28} />
            <p>{t("cards.need_soffice")}</p>
            <button className="btn btn-sm btn-primary" onClick={() => void openInSystem(item.path)}>
              <ExternalLink size={13} /> {t("cards.open_system")}
            </button>
          </div>
        )}
        {!err && kind === "office" && !loadingOffice && !needSoffice && !blobUrl && (
          <div className="preview-empty">
            <FileText size={28} />
            <p>{t("cards.office_hint")}</p>
            <button className="btn btn-sm btn-primary" onClick={() => void openInSystem(item.path)}>
              <ExternalLink size={13} /> {t("cards.open_system")}
            </button>
          </div>
        )}
        {!err && (mode === "code" || (kind === "code" && !blobUrl && !loadingOffice && !needSoffice)) && (
          <pre className="preview-code">{item.content || serverText || "…"}</pre>
        )}
        {!err && (kind === "image" || kind === "pdf") && !blobUrl && (
          <div className="preview-empty">
            <ImageIcon size={28} className="preview-spin" />
            <p>{t("cards.preview_loading")}</p>
          </div>
        )}
        {!err && mode === "image" && blobUrl && (
          <img className="preview-image" src={blobUrl} alt={item.name} />
        )}
        {!err && mode === "pdf" && blobUrl && (
          <iframe className="preview-pdf" title={item.name} src={blobUrl} />
        )}
        {!err && kind === "unknown" && (
          <div className="preview-empty">
            <FileCode size={28} />
            <p>{t("cards.preview_unsupported")}</p>
            <button className="btn btn-sm btn-primary" onClick={() => void openInSystem(item.path)}>
              {t("cards.open_system")}
            </button>
          </div>
        )}
      </div>
    </aside>
  );
});

export default PreviewPanel;
export { openInSystem, kindOf };
