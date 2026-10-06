import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "../i18n";
import { authFetch } from "../utils/api";
import {
  fmtBigTokens,
  heatColor,
  heatGrid,
  monthLabel,
  pivotModelSeries,
  type DashboardData,
  type HeatMode,
} from "../utils/usageStats";
import { fmtDuration, shortModel } from "../utils/turnHistory";

/** 趋势线的模型配色（按总量降序取用）。 */
const PALETTE = ["#4c8dff", "#34c77b", "#8b5cf6", "#f87171",
  "#f0a94b", "#22d3ee", "#f472b6", "#a3e635"];

const HEAT_WEEKS = 26;

/** 热力图格子所在日期：列起始（周一）+ 行偏移天数。 */
function cellDate(start: string, rowIdx: number): string {
  const t = new Date(start + "T00:00:00Z").getTime() + rowIdx * 86_400_000;
  return new Date(t).toISOString().slice(0, 10);
}

function Card({ value, label, sub }: { value: string; label: string; sub?: string }) {
  return (
    <div className="usage-card">
      <div className="usage-card-val">{value}</div>
      <div className="usage-card-label">{label}</div>
      {sub ? <div className="usage-card-sub">{sub}</div> : null}
    </div>
  );
}

/**
 * 整页"使用统计"（2026-10-04，对齐 ZCode 使用统计页的形态）：
 * 五张指标卡（累计/峰值/最长单轮/连续天数）、Token 活动热力图（每日/每周/累计）、
 * 按模型分线的每日趋势图（近 7 日/近 30 日）。数据全部来自
 * /v1/turn-metrics/dashboard。
 *
 * 取数策略（2026-10-05 修"页面永远空"）：只在页面**激活**时拉取，带退避重试，
 * 失败给显式重试按钮。此前是挂载即拉一次——面板常驻 DOM、应用启动时就挂载，
 * 请求与侧车启动赛跑，输了就永远空着（无重试、切页也不重拉），实测更新后两次
 * 打开统计页都是空的（后端 83 轮数据完好）。
 */
export default function UsageStatsView({ active = true }: { active?: boolean }) {
  const { t, lang } = useTranslation();
  const [dash, setDash] = useState<DashboardData | null>(null);
  const [failed, setFailed] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [mode, setMode] = useState<HeatMode>("daily");
  const [range, setRange] = useState<7 | 30>(7);
  const [clearing, setClearing] = useState(false);

  /** 清空统计（破坏性，二次确认）。只删用量记录——会话/记忆不受影响。 */
  const handleClear = async () => {
    const { ask } = await import("@tauri-apps/plugin-dialog");
    const ok = await ask(t("stats.clear_confirm"), { title: t("stats.clear_btn") });
    if (!ok) return;
    setClearing(true);
    try {
      const r = await authFetch("/v1/turn-metrics", { method: "DELETE" });
      const d = await r.json();
      if (d && d.status === "ok") {
        setReloadKey((k) => k + 1);   // 立即重拉 → 归零视图
      }
    } catch { /* 失败保持现状，下次进页重拉 */ }
    finally { setClearing(false); }
  };

  useEffect(() => {
    if (!active) return;
    let alive = true;
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tryLoad = async () => {
      try {
        const r = await authFetch("/v1/turn-metrics/dashboard?window=182");
        const d = await r.json();
        if (!alive) return;
        if (d && d.status === "ok") {
          setDash(d as DashboardData);
          setFailed(false);
          return;
        }
        throw new Error("dashboard: bad response");
      } catch {
        if (!alive) return;
        attempt += 1;
        if (attempt <= 5) {
          // 侧车可能还没监听（刚启动）：退避重试，别把失败留在页面上
          timer = setTimeout(() => { void tryLoad(); }, 800 * attempt);
        } else {
          setFailed(true);
        }
      }
    };
    void Promise.resolve().then(tryLoad);
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [active, reloadKey]);

  const perDay = useMemo(() => dash?.per_day || [], [dash]);

  // 趋势图：连续日期轴（缺数据的天补 0）
  const { dates, series } = useMemo(() => {
    const out: string[] = [];
    const dayMs = 86400000;
    const today = new Date();
    for (let i = range - 1; i >= 0; i--) {
      out.push(new Date(today.getTime() - i * dayMs).toISOString().slice(0, 10));
    }
    const piv = pivotModelSeries(dash?.per_day_model || [], out);
    return { dates: out, series: piv.slice(0, PALETTE.length) };
  }, [dash, range]);

  const heat = useMemo(() => heatGrid(perDay, mode, HEAT_WEEKS), [perDay, mode]);
  const maxCell = Math.max(1, heat.max);
  const totalTokens = (dash?.all_time.input || 0) + (dash?.all_time.gen || 0);
  const peak = dash?.peak_day;
  const maxSeriesVal = Math.max(1, ...series.flatMap((s) => s.values));

  // 悬停提示（2026-10-05）：原生 title 在 Tauri 的 WKWebView 里不弹（SVG <title>
  // 同样不可靠，且圆点命中区只有两三个像素）→ 受控 tooltip：hover 事件记下
  // 屏幕坐标 + 多行内容，fixed 定位小卡片跟随显示（pointer-events:none 防闪烁）。
  const [tip, setTip] = useState<{ x: number; y: number; lines: string[] } | null>(null);
  const dayByDate = useMemo(
    () => new Map(perDay.map((d) => [d.date, d])),
    [perDay],
  );
  const showTip = (e: React.MouseEvent, lines: string[]) => {
    if (!lines.length) { setTip(null); return; }
    setTip({ x: e.clientX, y: e.clientY, lines });
  };
  const hideTip = () => setTip(null);
  // 提示卡片位置：光标右下 12px，贴右边缘时翻到左侧
  const tipStyle = tip ? {
    left: tip.x + window.innerWidth - tip.x > 240 ? tip.x + 12 : Math.max(4, tip.x - 228),
    top: tip.y + 14,
  } : undefined;

  // 空态三义：读取中 / 读取失败（可重试）/ 已读但确无数据——不再混成一句"暂无数据"
  const placeholder = !dash ? (
    failed ? (
      <div className="usage-empty">
        {t("stats.load_failed")}
        <button type="button" className="usage-retry"
          onClick={() => setReloadKey((k) => k + 1)}>
          {t("stats.retry")}
        </button>
      </div>
    ) : (
      <div className="usage-empty">{t("stats.loading")}</div>
    )
  ) : (
    <div className="usage-empty">{t("stats.empty")}</div>
  );

  return (
    <div className="usage-view">
      <div className="usage-cards">
        <Card value={fmtBigTokens(totalTokens, lang)}
          label={t("stats.card_total")}
          sub={t("stats.card_n_turns", { n: dash?.all_time.n ?? 0 })} />
        <Card value={peak ? fmtBigTokens(peak.input + peak.gen, lang) : "0"}
          label={t("stats.card_peak")}
          sub={peak ? peak.date.slice(5) : undefined} />
        <Card value={fmtDuration(dash?.all_time.longest_turn_ms || 0)}
          label={t("stats.card_longest")} />
        <Card value={`${dash?.streak_current ?? 0} ${t("stats.unit_day")}`}
          label={t("stats.card_streak_cur")} />
        <Card value={`${dash?.streak_longest ?? 0} ${t("stats.unit_day")}`}
          label={t("stats.card_streak_max")} />
      </div>

      <div className="usage-panel">
        <div className="usage-panel-head">
          <span className="usage-panel-title">{t("stats.heatmap")}</span>
          <div className="usage-panel-tools">
            <div className="usage-seg">
              {(["daily", "weekly", "cumulative"] as HeatMode[]).map((m) => (
                <button key={m} type="button"
                  className={`usage-seg-btn${mode === m ? " active" : ""}`}
                  onClick={() => setMode(m)}>
                  {t(`stats.mode_${m}`)}
                </button>
              ))}
            </div>
            <button type="button" className="usage-clear-btn"
              disabled={clearing || !dash?.all_time.n}
              title={t("stats.clear_hint")}
              onClick={() => void handleClear()}>
              {clearing ? t("stats.clearing") : t("stats.clear_btn")}
            </button>
          </div>
        </div>
        {perDay.length === 0 ? placeholder : (
          <>
            <div className="usage-heat">
              {heat.columns.map((col, ci) => {
                const prev = ci > 0 ? heat.columns[ci - 1].start : "";
                const showMonth = monthLabel(col.start, lang) !== monthLabel(prev, lang);
                return (
                  <div className="usage-heat-col" key={col.start}>
                    <div className="usage-heat-month">
                      {showMonth ? monthLabel(col.start, lang) : ""}
                    </div>
                    {col.cells.map((v, ri) => {
                      const date = cellDate(col.start, ri);
                      const day = dayByDate.get(date);
                      return (
                        <i key={ri}
                          className={`usage-heat-cell${v === null ? " blank" : ""}`}
                          style={v ? { background: heatColor(v, maxCell) } : undefined}
                          onMouseEnter={(e) => {
                            if (!day || (day.input + day.gen) <= 0) { hideTip(); return; }
                            showTip(e, [
                              date,
                              `${t("chat.ctx_hist_input")} ${fmtBigTokens(day.input, lang)}`,
                              `${t("chat.ctx_hist_gen")} ${fmtBigTokens(day.gen, lang)}`,
                              `${t("chat.ctx_hist_turns")} ${day.n}`,
                            ]);
                          }}
                          onMouseLeave={hideTip} />
                      );
                    })}
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>

      <div className="usage-panel">
        <div className="usage-panel-head">
          <span className="usage-panel-title">{t("stats.trend")}</span>
          <div className="usage-seg">
            {([7, 30] as const).map((n) => (
              <button key={n} type="button"
                className={`usage-seg-btn${range === n ? " active" : ""}`}
                onClick={() => setRange(n)}>
                {t("stats.range_n", { n })}
              </button>
            ))}
          </div>
        </div>
        {series.length === 0 ? placeholder : (
          <>
            <div className="usage-legend">
              {series.map((s, i) => (
                <span className="usage-legend-item" key={s.model}>
                  <i style={{ background: PALETTE[i % PALETTE.length] }} />
                  {shortModel(s.model)}
                </span>
              ))}
            </div>
            <svg className="usage-trend" viewBox="0 0 600 170" preserveAspectRatio="none">
              {[0.25, 0.5, 0.75].map((f) => (
                <line key={f} x1={0} x2={600} y1={170 * f} y2={170 * f}
                  className="usage-trend-grid" />
              ))}
              {series.map((s, si) => {
                const pts = s.values.map((v, i) => {
                  const x = dates.length > 1 ? (i / (dates.length - 1)) * 596 + 2 : 300;
                  const y = 166 - (v / maxSeriesVal) * 156;
                  return `${x},${y}`;
                }).join(" ");
                return (
                  <polyline key={s.model} points={pts} fill="none"
                    stroke={PALETTE[si % PALETTE.length]}
                    strokeWidth={2} strokeLinejoin="round" strokeLinecap="round"
                    vectorEffect="non-scaling-stroke" />
                );
              })}
              {series.map((s, si) => s.values.map((v, i) => {
                const cx = dates.length > 1 ? (i / (dates.length - 1)) * 596 + 2 : 300;
                const cy = 166 - (v / maxSeriesVal) * 156;
                return (
                  <g key={`${s.model}:${i}`}>
                    <circle cx={cx} cy={cy} r={2.5} fill={PALETTE[si % PALETTE.length]} />
                    {/* 透明大热区：r=2.5 的点在 preserveAspectRatio=none 下命中区只有两三像素，
                        悬停根本碰不到（2026-10-05 tooltip 不弹的第二个原因） */}
                    <circle cx={cx} cy={cy} r={9} fill="transparent"
                      style={{ pointerEvents: "all" }}
                      onMouseEnter={(e) => showTip(e, [
                        dates[i],
                        `${shortModel(s.model)} · ${fmtBigTokens(v, lang)}`,
                      ])}
                      onMouseLeave={hideTip} />
                  </g>
                );
              }))}
            </svg>
            <div className="usage-trend-axis">
              <span>{dates[0]?.slice(5)}</span>
              <span>{dates[dates.length - 1]?.slice(5)}</span>
            </div>
          </>
        )}
      </div>

      {tip && (
        <div className="usage-tip" style={tipStyle}>
          {tip.lines.map((ln, i) => (
            <div key={i} className={i === 0 ? "usage-tip-date" : ""}>{ln}</div>
          ))}
        </div>
      )}
    </div>
  );
}
