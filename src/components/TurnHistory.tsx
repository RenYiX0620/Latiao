import { useTranslation } from "../i18n";
import {
  endedIcon,
  fmtDuration,
  fmtTime,
  fmtTokens,
  fmtTtft,
  shortModel,
  type TurnHistoryData,
} from "../utils/turnHistory";

/**
 * 历史轮次用量面板（2026-10-04）：turn_metrics 落库数据的查看界面。
 *
 * 纯展示组件：数据与请求由 ContextMeter 持有（在"历史用量"点击时拉取——
 * react-hooks/set-state-in-effect 不允许挂载即 setState，且与 ContextMeter
 * "悬浮才拉、按需请求"的哲学一致）。聚合在后端 /v1/turn-metrics/summary。
 */
export default function TurnHistory({ data, scopeAll, onToggleScope }: {
  data: TurnHistoryData | null;
  scopeAll: boolean;
  onToggleScope: () => void;
}) {
  const { t, lang } = useTranslation();

  const s = data?.summary;
  const bars = s?.per_day || [];
  const maxGen = Math.max(1, ...bars.map((b) => b.gen));
  const recent = data?.recent || [];

  return (
    <div className="ctx-hist">
      <div className="ctx-hist-head">
        <span className="ctx-title">{t("chat.ctx_hist_title")}</span>
        <button
          type="button"
          className="ctx-hist-scope"
          onClick={onToggleScope}
          title={t("chat.ctx_hist_scope_hint")}
        >
          {scopeAll ? t("chat.ctx_hist_scope_all") : t("chat.ctx_hist_scope_session")}
        </button>
      </div>

      {!s ? (
        <div className="ctx-empty">{t("chat.ctx_hist_loading")}</div>
      ) : (
        <>
          <div className="ctx-hist-grid">
            <span className="ctx-row-label">{t("chat.ctx_hist_turns")}</span>
            <span className="ctx-row-val">
              {s.n_turns_total}
              {s.agg_capped ? ` (${t("chat.ctx_hist_cap", { n: s.n_rows_aggregated })})` : ""}
            </span>
            <span className="ctx-row-label">{t("chat.ctx_hist_local_cloud")}</span>
            <span className="ctx-row-val">
              {t("chat.ctx_hist_local")}{s.local_turns} · {t("chat.ctx_hist_cloud")}{s.cloud_turns}
            </span>
            <span className="ctx-row-label">{t("chat.ctx_hist_input")}</span>
            <span className="ctx-row-val">{fmtTokens(s.totals.input, lang)}</span>
            <span className="ctx-row-label">{t("chat.ctx_hist_gen")}</span>
            <span className="ctx-row-val">{fmtTokens(s.totals.gen, lang)}</span>
            <span className="ctx-row-label">{t("chat.ctx_hist_refine")}</span>
            <span className="ctx-row-val">
              {s.totals.refine_calls}（{fmtTokens(s.totals.refine_tokens, lang)}）
            </span>
            <span className="ctx-row-label">{t("chat.ctx_hist_retries")}</span>
            <span className="ctx-row-val">{s.totals.retries}</span>
            <span className="ctx-row-label">{t("chat.ctx_hist_ttft")}</span>
            <span className="ctx-row-val">{fmtTtft(s.avg_ttft_ms_local)}</span>
            <span className="ctx-row-label">{t("chat.ctx_hist_ended")}</span>
            <span className="ctx-row-val">
              {Object.entries(s.ended)
                .sort((a, b) => b[1] - a[1])
                .map(([k, v]) => `${endedIcon(k)}${v}`)
                .join(" · ") || "—"}
            </span>
          </div>

          {bars.length > 0 && (
            <div className="ctx-hist-bars" aria-hidden="true">
              {bars.map((b) => (
                <i
                  key={b.date}
                  className="ctx-hist-bar"
                  style={{ height: `${Math.max(4, Math.round((b.gen / maxGen) * 100))}%` }}
                  title={`${b.date} · ${t("chat.ctx_hist_input")} ${fmtTokens(b.input, lang)} · ${t("chat.ctx_hist_gen")} ${fmtTokens(b.gen, lang)} · ${b.n}`}
                />
              ))}
            </div>
          )}
          {bars.length > 1 && (
            <div className="ctx-hist-axis">
              <span>{bars[0].date.slice(5)}</span>
              <span>{bars[bars.length - 1].date.slice(5)}</span>
            </div>
          )}

          <div className="ctx-hist-recent-title">{t("chat.ctx_hist_recent")}</div>
          {recent.length === 0 ? (
            <div className="ctx-empty">{t("chat.ctx_hist_empty")}</div>
          ) : (
            <div className="ctx-hist-body">
              <table className="ctx-hist-table">
                <thead>
                  <tr>
                    <th>{t("chat.ctx_hist_col_time")}</th>
                    <th>{t("chat.ctx_hist_col_model")}</th>
                    <th className="num">{t("chat.ctx_hist_col_input")}</th>
                    <th className="num">{t("chat.ctx_hist_col_gen")}</th>
                    <th className="num">{t("chat.ctx_hist_col_ttft")}</th>
                    <th className="num">{t("chat.ctx_hist_col_duration")}</th>
                    <th>{t("chat.ctx_hist_col_end")}</th>
                  </tr>
                </thead>
                <tbody>
                  {recent.map((r) => (
                    <tr key={r.id}>
                      <td>{fmtTime(r.started_at)}</td>
                      <td title={r.model}>
                        {r.is_local ? "" : "☁ "}{shortModel(r.model)}
                      </td>
                      <td className="num">{fmtTokens(r.input_tokens, lang)}</td>
                      <td className="num">{fmtTokens(r.gen_tokens, lang)}</td>
                      <td className="num">{r.is_local ? fmtTtft(r.ttft_ms) : "—"}</td>
                      <td className="num">{fmtDuration(r.duration_ms)}</td>
                      <td title={r.ended_reason}>{endedIcon(r.ended_reason)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
