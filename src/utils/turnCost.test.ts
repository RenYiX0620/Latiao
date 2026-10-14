import { describe, expect, it } from "vitest";
import T from "../i18n/translations";
import { turnCostView, type TurnCostLike } from "./turnCost";

/** 与 useTranslation 同口径的最小 t（zh），只为验证取键与占位替换。 */
const t = (key: string, vars?: Record<string, string | number>) => {
  let text = T[key]?.zh || key;
  if (vars) for (const [k, v] of Object.entries(vars)) text = text.split(`{${k}}`).join(String(v));
  return text;
};
const fmt = (n: number) => `${n}`;

const base: TurnCostLike = {
  input_tokens: 128_000,
  gen_tokens: 3_200,
  budget: 400_000,
  budget_percent: 32,
  retries: 2,
  retry_kinds: ["empty_generation", "lang_drift"],
  refine_calls: 9,
  refine_tokens: 4_100,
};

describe("本轮花费展示（成本可见）", () => {
  it("无数据 → 整块隐藏（旧会话/未开始不该出现空行）", () => {
    expect(turnCostView(null, fmt, t)).toBeNull();
    expect(turnCostView({ ...base, input_tokens: 0, gen_tokens: 0, retries: 0,
                          refine_calls: 0 }, fmt, t)).toBeNull();
  });

  it("有预算 → 已用/预算（百分比）", () => {
    const v = turnCostView(base, fmt, t)!;
    expect(v.input).toBe("128000 / 400000（32%）");
  });

  it("预算关闭（0）→ 只报已用", () => {
    const v = turnCostView({ ...base, budget: 0, budget_percent: null }, fmt, t)!;
    expect(v.input).toBe("128000 token");
  });

  it("开销行含 生成/重采/知识提炼，重采原因单列", () => {
    const v = turnCostView(base, fmt, t)!;
    expect(v.cost).toContain("生成 3200");
    expect(v.cost).toContain("重采 2 次");
    expect(v.cost).toContain("知识提炼 9 次（4100）");
    expect(v.retryKinds).toContain("empty_generation, lang_drift");
  });

  it("没有重采 → 不给原因行", () => {
    const v = turnCostView({ ...base, retries: 0, retry_kinds: [] }, fmt, t)!;
    expect(v.retryKinds).toBeNull();
  });

  it("来源行：只有一类来源时不显示（避免噪音）", () => {
    const v = turnCostView({ ...base, by_source: {
      main_turn: { requests: 3, input_tokens: 12000, output_tokens: 300 },
    } }, fmt, t)!;
    expect(v.sources).toBeNull();
  });

  it("来源行：主循环 + 子代理 + 知识提炼各一片（子代理带次数）", () => {
    const v = turnCostView({ ...base, by_source: {
      main_turn: { requests: 3, input_tokens: 12000, output_tokens: 300 },
      subagent: { requests: 2, input_tokens: 3000, output_tokens: 100 },
      refine: { requests: 9, input_tokens: 400, output_tokens: 60 },
    } }, fmt, t)!;
    expect(v.sources).toContain("主循环 12300");
    expect(v.sources).toContain("子代理 3100（2 次）");
    expect(v.sources).toContain("知识提炼 460（9 次）");
  });

  it("by_source 缺省（旧后端）→ 不给来源行，不炸", () => {
    expect(turnCostView(base, fmt, t)!.sources).toBeNull();
  });

  it("有子代理开销 → 输入行注明不含子代理（预算口径）", () => {
    const v = turnCostView({ ...base, by_source: {
      main_turn: { requests: 1, input_tokens: 12000, output_tokens: 300 },
      subagent: { requests: 2, input_tokens: 3000, output_tokens: 100 },
    } }, fmt, t)!;
    expect(v.input).toContain("128000 / 400000（32%）");
    expect(v.input).toContain("（不含子代理）");
  });

  it("没有子代理开销 → 输入行不加提示（不制造噪音）", () => {
    const v = turnCostView({ ...base, by_source: {
      main_turn: { requests: 1, input_tokens: 12000, output_tokens: 300 },
    } }, fmt, t)!;
    expect(v.input).not.toContain("不含子代理");
  });
});
