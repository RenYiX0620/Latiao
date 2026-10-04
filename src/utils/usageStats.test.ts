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
  const days: DashDay[] = DATES.map((d, i) => ({
    date: d, n: 1, input: (i + 1) * 100, gen: 0,
  }));
  it("daily 模式取当日量；窗口外为 null", () => {
    const { columns, max } = heatGrid(days, "daily", 1);
    // 1 列 = 最近一周；末格是今天（2026-… 假设今天不是 9-28/29/30，则命中的只有窗口里的那几天）
    const flat = columns[0].cells.filter((x): x is number => x !== null);
    const got = flat.filter((v) => v > 0);
    expect(got.every((v) => [100, 200, 300].includes(v))).toBe(true);
    expect(max).toBe(300);
  });
  it("cumulative 模式单调递增", () => {
    const { columns } = heatGrid(days, "cumulative", 1);
    const vals = columns[0].cells.filter((x): x is number => x !== null && x > 0);
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
