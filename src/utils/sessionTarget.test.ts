import { describe, expect, it } from "vitest";
import { resolveStopTarget } from "./sessionTarget";

describe("resolveStopTarget（崩溃防护）", () => {
  it("字符串就用它", () => {
    expect(resolveStopTarget("session_a", "session_fallback")).toBe("session_a");
  });

  it("空/空白字符串回落（不然会 POST 空 id）", () => {
    expect(resolveStopTarget("", "fb")).toBe("fb");
    expect(resolveStopTarget("   ", "fb")).toBe("fb");
  });

  it("React 事件对象（带循环引用）必须被挡下 —— 事故就是它", () => {
    const evt: Record<string, unknown> = { type: "click", target: {} };
    evt.self = evt; // 循环引用：JSON.stringify 会抛
    expect(resolveStopTarget(evt, "fb")).toBe("fb");
    expect(() => JSON.stringify(evt)).toThrow();   // 证明这个入参真的会炸
  });

  it("undefined / null / 会话对象 都回落", () => {
    expect(resolveStopTarget(undefined, "fb")).toBe("fb");
    expect(resolveStopTarget(null, "fb")).toBe("fb");
    expect(resolveStopTarget({ id: "session_a" }, "fb")).toBe("fb");
  });
});
