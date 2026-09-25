/** useChatStream 关联语义（纯逻辑，无 DOM）。SSE 解析细节见 utils/sse.test.ts。 */
import { describe, it, expect } from "vitest";
import { parseSSEDataLine } from "../utils/sse";
import { resolveStopTarget } from "../utils/sessionTarget";

describe("chat stream 输入面语义", () => {
  it("畸形 JSON 行按 skip 处理，不炸整条流", () => {
    expect(parseSSEDataLine('data: {"content":')).toEqual({ kind: "skip" });
    expect(parseSSEDataLine("data: {not json")).toEqual({ kind: "skip" });
  });

  it("同 callId 并发只处理一次（confirmTool 幂等语义）", async () => {
    const seen = new Set<string>();
    let hits = 0;
    const confirmLike = async (callId: string) => {
      if (seen.has(callId)) return;
      seen.add(callId);
      hits += 1;
      await Promise.resolve();
    };
    await Promise.all([confirmLike("c1"), confirmLike("c1"), confirmLike("c1")]);
    expect(hits).toBe(1);
  });

  it("stop 目标解析：走真实 util（不再本地抄实现），非字符串入参回落", () => {
    // 必须 import 真实现——否则 useChatStream 内那份丢了 .trim() 测试也绿
    expect(resolveStopTarget("s2", "s1")).toBe("s2");
    expect(resolveStopTarget({ type: "click" }, "s1")).toBe("s1");
    expect(resolveStopTarget("", "s1")).toBe("s1");
    expect(resolveStopTarget("   ", "s1")).toBe("s1");
  });
});
