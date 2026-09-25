import { useCallback, useMemo, useRef, useState } from "react";
import { useTranslation } from "../i18n";
import { authFetch } from "../utils/api";

/** 与后端 context_stats.CATEGORIES 一一对应（顺序即展示顺序）。 */
const ROWS: { key: string; i18n: string; color: string }[] = [
  { key: "messages", i18n: "chat.ctx_messages", color: "#4c8dff" },
  { key: "system_tools", i18n: "chat.ctx_system_tools", color: "#37c2ff" },
  { key: "skills", i18n: "chat.ctx_skills", color: "#8b7cff" },
  { key: "system_prompt", i18n: "chat.ctx_system_prompt", color: "#f0a94b" },
  { key: "mcp_tools", i18n: "chat.ctx_mcp", color: "#4fd1a5" },
  { key: "other", i18n: "chat.ctx_other", color: "#8b93a7" },
];

interface CtxStats {
  available: boolean;
  limit: number;
  limit_source: string;
  total: number;
  percent: number | null;
  breakdown: { key: string; tokens: number; percent: number }[];
  cache_hit_rate: number | null;
  cache_samples: number;
  token_source: string;
}

/** token 数按语言习惯缩写：中文用「万」，其余用 k/M（对齐同类工具的面板写法）。 */
function fmtTokens(n: number, lang: string): string {
  if (!n) return "0";
  if (lang === "zh") {
    if (n >= 10000) {
      const w = n / 10000;
      return `${w >= 100 ? Math.round(w) : w.toFixed(1).replace(/\.0$/, "")}万`;   // 仅 zh 分支走到这里
    }
    return n.toLocaleString();
  }
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return n.toLocaleString();
}

/**
 * 上下文容量指示器 + 悬浮面板。
 *
 * 数据来自 /v1/context/stats（后端在组装请求时按类别快照"这一轮实际发出的内容"）：
 * 容量、六类占比、平均缓存命中率。取不到数据时退回会话里已有的字符估算，仅在
 * 悬浮时拉取（2 秒内不重复请求），避免给聊天主链路加负担。
 */
export default function ContextMeter({ sessionId, fallbackTokens, fallbackLimit }: {
  sessionId?: string;
  fallbackTokens: number;
  fallbackLimit?: number | null;
}) {
  const { t, lang } = useTranslation();
  const [data, setData] = useState<CtxStats | null>(null);
  const [open, setOpen] = useState(false);
  const fetchedAt = useRef(0);

  const load = useCallback(async () => {
    if (!sessionId) return;
    try {
      const r = await authFetch(`/v1/context/stats?session_id=${encodeURIComponent(sessionId)}`);
      const d = await r.json();
      if (d && d.status === "ok") {
        setData(d as CtxStats);
        fetchedAt.current = Date.now();
      }
    } catch {
      /* 侧车未就绪：保持本地估算，不打扰用户 */
    }
  }, [sessionId]);

  const show = useCallback(() => {
    setOpen(true);
    if (Date.now() - fetchedAt.current > 2000) void load();
  }, [load]);

  const live = !!data?.available;
  const total = live ? data!.total : fallbackTokens;
  const limit = (live && data!.limit) || fallbackLimit || 0;
  const percent = limit ? Math.round((total * 1000) / limit) / 10 : null;
  const cache = live ? data!.cache_hit_rate : null;

  const rows = useMemo(() => {
    const byKey: Record<string, number> = {};
    for (const b of data?.breakdown || []) byKey[b.key] = b.percent;
    return ROWS.map((r) => ({ ...r, pct: byKey[r.key] ?? 0 }));
  }, [data]);

  return (
    <span className="ctx-meter" onMouseEnter={show} onMouseLeave={() => setOpen(false)}>
      <button type="button" className="ctx-meter-btn" title={t("chat.ctx_title")}>
        {cache === null
          ? t("chat.ctx_cache_na")
          : t("chat.ctx_cache_rate", { percent: (cache * 100).toFixed(0) })}
      </button>
      {open && (
        <div className="ctx-panel" role="tooltip">
          <div className="ctx-head">
            <span className="ctx-title">{t("chat.ctx_title")}</span>
            <span className="ctx-cap">
              {fmtTokens(total, lang)}/{limit ? fmtTokens(limit, lang) : "—"}
              {percent !== null ? `（${percent}%）` : ""}
            </span>
          </div>
          <div className="ctx-stack" aria-hidden="true">
            {live && rows.map((r) => (
              <i key={r.key} style={{ width: `${r.pct}%`, background: r.color }} />
            ))}
          </div>
          {live ? (
            <div className="ctx-rows">
              {rows.map((r) => (
                <div className="ctx-row" key={r.key}>
                  <span className="ctx-dot" style={{ background: r.color }} />
                  <span className="ctx-row-label">{t(r.i18n)}</span>
                  <span className="ctx-row-val">{r.pct.toFixed(1)}%</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="ctx-empty">{t("chat.ctx_no_data")}</div>
          )}
          <div className="ctx-foot">
            <span>{t("chat.ctx_cache")}</span>
            <span>
              {cache === null
                ? t("chat.ctx_cache_na")
                : `${(cache * 100).toFixed(1)}%${data!.cache_samples > 1 ? ` · ${data!.cache_samples}x` : ""}`}
            </span>
          </div>
        </div>
      )}
    </span>
  );
}
