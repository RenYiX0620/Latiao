import { useCallback, useMemo, useRef, useState } from "react";
import { useTranslation } from "../i18n";
import { authFetch } from "../utils/api";
import { fmtTokens, type TurnHistoryData } from "../utils/turnHistory";
import { turnCostView, type TurnCostLike } from "../utils/turnCost";
import TurnHistory from "./TurnHistory";

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
  /** 成本可见（gap 第 4 步）：本轮花了多少、花在哪 */
  turn?: TurnCostLike | null;
}

/** token 数缩写逻辑已上移 src/utils/turnHistory.ts（历史面板共用同一份）。 */

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
  const [showHist, setShowHist] = useState(false);
  const [histData, setHistData] = useState<TurnHistoryData | null>(null);
  const [histScopeAll, setHistScopeAll] = useState(true);
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

  /** 历史用量：只在点击（切入口/切范围）时拉取——react-hooks 不允许挂载即
   * setState，且与"按需请求"的哲学一致。 */
  const loadHist = useCallback(async (scopeAll: boolean) => {
    const sid = scopeAll ? "" : (sessionId || "");
    const q = sid ? `?session_id=${encodeURIComponent(sid)}` : "";
    try {
      const r = await authFetch(`/v1/turn-metrics/summary${q}`);
      const d = await r.json();
      if (d && d.status === "ok") setHistData(d as TurnHistoryData);
    } catch {
      /* 侧车未就绪：面板保持空态 */
    }
  }, [sessionId]);

  const toggleHist = useCallback(() => {
    setShowHist((v) => {
      if (!v) void loadHist(true);
      return !v;
    });
  }, [loadHist]);

  const toggleHistScope = useCallback(() => {
    setHistScopeAll((v) => {
      void loadHist(!v);
      return !v;
    });
  }, [loadHist]);

  const live = !!data?.available;
  const total = live ? data!.total : fallbackTokens;
  const limit = (live && data!.limit) || fallbackLimit || 0;
  const percent = limit ? Math.round((total * 1000) / limit) / 10 : null;
  const cache = live ? data!.cache_hit_rate : null;

  const turnView = useMemo(
    () => turnCostView(data?.turn, (n) => fmtTokens(n, lang), t),
    [data, lang, t],
  );

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
          <button
            type="button"
            className="ctx-hist-toggle"
            onClick={toggleHist}
          >
            {showHist ? t("chat.ctx_hist_back") : t("chat.ctx_hist_toggle")}
          </button>
          {showHist ? (
            <TurnHistory data={histData} scopeAll={histScopeAll} onToggleScope={toggleHistScope} />
          ) : (
            <>
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
          {live && turnView && (
            <div className="ctx-turn">
              <div className="ctx-row">
                <span className="ctx-row-label">{t("chat.ctx_turn_input")}</span>
                <span className="ctx-row-val">{turnView.input}</span>
              </div>
              <div className="ctx-row">
                <span className="ctx-row-label">{t("chat.ctx_turn_cost")}</span>
                <span className="ctx-row-val">{turnView.cost}</span>
              </div>
              {turnView.retryKinds && (
                <div className="ctx-row-val" style={{ opacity: 0.7 }}>
                  {turnView.retryKinds}
                </div>
              )}
              {turnView.sources && (
                <div className="ctx-row">
                  <span className="ctx-row-label">{t("chat.ctx_turn_sources")}</span>
                  <span className="ctx-row-val">{turnView.sources}</span>
                </div>
              )}
            </div>
          )}
          <div className="ctx-foot">
            <span>{t("chat.ctx_cache")}</span>
            <span>
              {cache === null
                ? t("chat.ctx_cache_na")
                : `${(cache * 100).toFixed(1)}%${data!.cache_samples > 1 ? ` · ${data!.cache_samples}x` : ""}`}
            </span>
          </div>
            </>
          )}
        </div>
      )}
    </span>
  );
}
