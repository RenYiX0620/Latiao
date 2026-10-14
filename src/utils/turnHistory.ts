/** 历史轮次用量的纯工具层（2026-10-04）：数据形状 + 格式化。
 *
 * 数据来自 /v1/turn-metrics/summary（sidecar/turn_metrics.py 落库、
 * api_routes_admin.get_turn_metrics_summary 聚合）。这里不做请求、不碰 React，
 * 便于 vitest 封闭测试。fmtTokens 从 ContextMeter 上移（两处共用）。 */

export interface TurnSourceBucket {
  input_tokens?: number;
  output_tokens?: number;
  calls?: number;
}

/** turn_metrics 表一行（list_turns 的返回形状）。 */
export interface TurnRow {
  id: string;
  session_id: string;
  model: string;
  is_local: number;
  started_at: string;
  duration_ms: number;
  input_tokens: number;
  gen_tokens: number;
  retries: number;
  steps: number;
  ttft_ms: number | null;
  refine_calls: number;
  refine_tokens: number;
  budget: number;
  by_source: Record<string, TurnSourceBucket>;
  ended_reason: string;
}

export interface TurnDay {
  date: string;
  n: number;
  input: number;
  gen: number;
}

export interface TurnSummary {
  n_turns_total: number;
  n_rows_aggregated: number;
  agg_capped: boolean;
  local_turns: number;
  cloud_turns: number;
  totals: {
    input: number;
    gen: number;
    retries: number;
    refine_calls: number;
    refine_tokens: number;
  };
  by_model: { model: string; is_local: boolean; n: number; input: number; gen: number }[];
  ended: Record<string, number>;
  avg_ttft_ms_local: number | null;
  per_day: TurnDay[];
}

export interface TurnHistoryData {
  status: string;
  summary: TurnSummary;
  recent: TurnRow[];
}

/** token 数按语言习惯缩写：中文用「万」，其余用 k/M（对齐同类工具的面板写法）。 */
export function fmtTokens(n: number, lang: string): string {
  if (!n) return "0";
  if (lang === "zh") {
    if (n >= 10000) {
      const w = n / 10000;
      return `${w >= 100 ? Math.round(w) : w.toFixed(1).replace(/\.0$/, "")}万`;
    }
    return n.toLocaleString();
  }
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return n.toLocaleString();
}

/** 轮耗时：<1 分钟给 "3.2s"，以上给 "2m05s"。语言中立。 */
export function fmtDuration(ms: number): string {
  if (!ms || ms < 0) return "—";
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s - m * 60);
  return `${m}m${String(rest).padStart(2, "0")}s`;
}

/** 模型名压缩到表格能放下：取路径末段、去 .gguf 后缀、超长截断。 */
export function shortModel(name: string): string {
  const base = String(name || "").split(/[\\/]/).pop() || String(name || "");
  const stem = base.replace(/\.gguf$/i, "");
  return stem.length > 24 ? `${stem.slice(0, 23)}…` : stem;
}

/** 结束原因 → 图标（颜色语义由 CSS 类给，不在此处）。未知原因给空心点。 */
export function endedIcon(reason: string): string {
  switch (String(reason || "")) {
    case "completed": return "✓";
    case "error": return "✗";
    case "aborted": return "⏹";
    case "cron": return "🕐";
    case "stale": return "⏱";
    default: return "•";
  }
}

/** started_at（后端本地时间的 naive ISO）→ "MM-DD HH:MM"。语言中立。 */
export function fmtTime(iso: string): string {
  const s = String(iso || "");
  return s.length >= 16 ? s.slice(5, 16).replace("T", " ") : s;
}

/** 首字延迟显示：秒一位小数；无数据显示 —。 */
export function fmtTtft(ms: number | null): string {
  if (ms === null || ms === undefined) return "—";
  return `${(ms / 1000).toFixed(1)}s`;
}
