import { describe, expect, it } from "vitest";
import {
  fmtBigTokens,
  heatColor,
  heatGrid,
  monthLabel,
  pivotModelSeries,
  type DashDay,
  type DashDayModel,
} from "./usageStats";

describe("fmtBigTokens", () => {
  it("中文亿/万分层，其他 B/M/k", () => {
    expect(fmtBigTokens(0, "zh")).toBe("0");
    expect(fmtBigTokens(6.59e9, "zh")).toBe("65.9亿");
    expect(fmtBigTokens(9.7e8, "zh")).toBe("9.7亿");
    expect(fmtBigTokens(15000, "zh")).toBe("1.5万");
    expect(fmtBigTokens(6.59e9, "en")).toBe("6.6B");
    expect(fmtBigTokens(970_000_000, "en")).toBe("970M");
  });
});

describe("heatColor", () => {
  it("0 与越界给空，占比进 4 档", () => {
    expect(heatColor(0, 100)).toBe("");
    expect(heatColor(5, 0)).toBe("");
    expect(heatColor(10, 100)).toBe("#1e3a8a");
    expect(heatColor(90, 100)).toBe("#93c5fd");
  });
});

const DATES = ["2026-09-28", "2026-09-29", "2026-09-30"];
const PM: DashDayModel[] = [
  { date: "2026-09-28", model: "A", tokens: 100 },
  { date: "2026-09-28", model: "B", tokens: 40 },
  { date: "2026-09-30", model: "A", tokens: 60 },
];

describe("pivotModelSeries", () => {
  it("对齐日期轴、缺日补 0、按总量降序", () => {
    const s = pivotModelSeries(PM, DATES);
    expect(s[0].model).toBe("A");
    expect(s[0].values).toEqual([100, 0, 60]);
    expect(s[1].model).toBe("B");
    expect(s[1].values).toEqual([40, 0, 0]);
  });
});

describe("heatGrid", () => {
  // 日期相对"今天"生成。曾经写死 2026-09-28..30：本地 10-04（周日）跑是绿的，
  // 一到 10-05（周一）周界滚动、写死的日期全部落到窗口外 → CI 上 max=0 挂掉。
  // 热力图的窗口跟随真实今天，测试就必须跟随真实今天。
  const dayMs = 86_400_000;
  const iso = (ms: number) => new Date(ms).toISOString().slice(0, 10);
  const now = Date.now();
  const RECENT = [iso(now - 2 * dayMs), iso(now - dayMs), iso(now)];
  const days: DashDay[] = RECENT.map((d, i) => ({
    date: d, n: 1, input: (i + 1) * 100, gen: 0,
  }));

  it("daily 模式取当日量（近三天都在 8 周窗口内）", () => {
    const { max } = heatGrid(days, "daily", 8);
    expect(max).toBe(300);
  });
  it("cumulative 模式单调递增", () => {
    const { columns } = heatGrid(days, "cumulative", 8);
    const vals = columns
      .flatMap((c) => c.cells)
      .filter((x): x is number => x !== null && x > 0);
    for (let i = 1; i < vals.length; i++) expect(vals[i]).toBeGreaterThanOrEqual(vals[i - 1]);
  });
  it("weekly 模式每列一个值", () => {
    const { columns } = heatGrid(days, "weekly", 2);
    expect(columns.length).toBe(2);
  });
});

describe("monthLabel", () => {
  it("中文月数字 / 英文缩写", () => {
    expect(monthLabel("2026-10-06", "zh")).toBe("10月");
    expect(monthLabel("2026-10-06", "en")).toBe("Oct");
  });
});

import { smoothPath } from "./usageStats";

describe("smoothPath", () => {
  it("单点/空：M 指令或空串", () => {
    expect(smoothPath([])).toBe("");
    expect(smoothPath([{ x: 5, y: 6 }])).toBe("M5,6");
  });
  it("多点：M 开头 + 每段一个 C，终点是最后一个点", () => {
    const pts = [{ x: 0, y: 10 }, { x: 10, y: 0 }, { x: 20, y: 10 }];
    const d = smoothPath(pts);
    expect(d.startsWith("M0,10")).toBe(true);
    expect((d.match(/ C/g) || []).length).toBe(2);
    expect(d.endsWith("20,10")).toBe(true);
  });
});
