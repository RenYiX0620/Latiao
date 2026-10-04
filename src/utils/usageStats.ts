/** 整页"使用统计"的纯工具层（2026-10-04，对齐 ZCode 使用统计页的形态）。
 *
 * 数据来自 /v1/turn-metrics/dashboard（sidecar/turn_metrics.dashboard_stats）。
 * 这里只做：大数格式化、热力图色阶、按天×模型透视、热力图三种模式
 * （每日/累计/每周）的取值——不请求、不碰 React。 */

export interface DashAllTime {
  n: number;
  input: number;
  gen: number;
  longest_turn_ms: number;
}
export interface DashDay {
  date: string;
  n: number;
  input: number;
  gen: number;
}
export interface DashDayModel {
  date: string;
  model: string;
  tokens: number;
}
export interface DashboardData {
  status: string;
  all_time: DashAllTime;
  per_day: DashDay[];
  per_day_model: DashDayModel[];
  peak_day: DashDay | null;
  streak_current: number;
  streak_longest: number;
  window_days: number;
}

/** 大数：中文亿/万，其他 B/M/k（65.9亿 那种卡片段落）。 */
export function fmtBigTokens(n: number, lang: string): string {
  if (!n || n <= 0) return "0";
  if (lang === "zh") {
    if (n >= 1e8) {
      const y = n / 1e8;
      return `${y >= 100 ? Math.round(y) : y.toFixed(1).replace(/\.0$/, "")}亿`;
    }
    if (n >= 1e4) {
      const w = n / 1e4;
      return `${w >= 100 ? Math.round(w) : w.toFixed(1).replace(/\.0$/, "")}万`;
    }
    return n.toLocaleString();
  }
  if (n >= 1e9) return `${(n / 1e9).toFixed(1).replace(/\.0$/, "")}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1).replace(/\.0$/, "")}k`;
  return n.toLocaleString();
}

export type HeatMode = "daily" | "cumulative" | "weekly";

/** 单元格颜色：0 = 空格；其余按占比进 4 档蓝阶（浅=多，对齐深色主题观感）。 */
export function heatColor(tokens: number, max: number): string {
  if (!tokens || tokens <= 0 || max <= 0) return "";
  const ratio = tokens / max;
  if (ratio > 0.75) return "#93c5fd";
  if (ratio > 0.5) return "#60a5fa";
  if (ratio > 0.25) return "#2563eb";
  return "#1e3a8a";
}

/** 按天×模型 → 每个模型一条与 dates 对齐的序列（缺日补 0），按总量降序。 */
export function pivotModelSeries(
  perDayModel: DashDayModel[],
  dates: string[],
): { model: string; values: number[]; total: number }[] {
  const idx = new Map<string, number>(dates.map((d, i) => [d, i]));
  const acc = new Map<string, number[]>();
  for (const r of perDayModel) {
    const i = idx.get(r.date);
    if (i === undefined) continue;
    let arr = acc.get(r.model);
    if (!arr) {
      arr = new Array(dates.length).fill(0);
      acc.set(r.model, arr);
    }
    arr[i] += r.tokens;
  }
  return [...acc.entries()]
    .map(([model, values]) => ({
      model,
      values,
      total: values.reduce((a, b) => a + b, 0),
    }))
    .sort((a, b) => b.total - a.total);
}

export interface HeatColumn {
  /** 列起始日期（周一），用于月标签 */
  start: string;
  /** 行内 7 格的值；窗口外的日子 = null（不渲染） */
  cells: (number | null)[];
}

/** 热力图网格：最近 weeks 周、列对齐到周一、最后一列含今天。
 * mode：daily=当日量；cumulative=窗口内累计到当日；weekly=整列合计。 */
export function heatGrid(
  perDay: DashDay[],
  mode: HeatMode,
  weeks = 26,
): { columns: HeatColumn[]; max: number } {
  const today = new Date();
  const dayMs = 86400000;
  // 结尾对齐到本周周日（含今天），起点 = 末列周一 - (weeks-1) 周
  const dow = (today.getDay() + 6) % 7; // 0=周一
  const weekEnd = today.getTime() + (6 - dow) * dayMs;
  const startMs = weekEnd - (weeks * 7 - 1) * dayMs;
  const byDate = new Map<string, number>(
    perDay.map((d) => [d.date, d.input + d.gen]),
  );
  // 累计模式的前缀和（按窗口内日期顺序）
  const sortedDates = [...byDate.keys()].sort();
  const cum = new Map<string, number>();
  let run = 0;
  for (const d of sortedDates) {
    run += byDate.get(d) || 0;
    cum.set(d, run);
  }
  const columns: HeatColumn[] = [];
  const values: number[] = [];
  for (let w = 0; w < weeks; w++) {
    const colStart = startMs + w * 7 * dayMs;
    const cells: (number | null)[] = [];
    let colSum = 0;
    let any = false;
    for (let r = 0; r < 7; r++) {
      const ms = colStart + r * dayMs;
      if (ms > today.getTime()) {
        cells.push(null);
        continue;
      }
      const d = new Date(ms).toISOString().slice(0, 10);
      let v: number;
      if (mode === "daily") v = byDate.get(d) || 0;
      else if (mode === "cumulative") v = cum.get(d) || 0;
      else v = byDate.get(d) || 0;
      if (byDate.has(d)) any = true;
      colSum += v;
      cells.push(v);
    }
    columns.push({ start: new Date(colStart).toISOString().slice(0, 10), cells });
    values.push(mode === "weekly" && any ? colSum : 0);
    if (mode !== "weekly") values.push(...cells.filter((x): x is number => x !== null));
  }
  const nums = mode === "weekly" ? values : values;
  const max = Math.max(0, ...nums);
  return { columns, max };
}

/** 月标签：列起始跨月时给 "M月"/"Mon"。 */
export function monthLabel(dateStr: string, lang: string): string {
  const m = Number(dateStr.slice(5, 7));
  if (!m) return "";
  const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return lang === "zh" ? `${m}月` : names[m - 1];
}
