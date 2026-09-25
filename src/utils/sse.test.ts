import { describe, it, expect } from "vitest";
import { parseSSEDataLine } from "./sse";

describe("parseSSEDataLine（SSE 流式协议解析）", () => {
  it("解析正常 data 行", () => {
    const r = parseSSEDataLine('data: {"event":"content","content":"hi"}');
    expect(r.kind).toBe("data");
    if (r.kind !== "data") return;
    expect(r.parsed.event).toBe("content");
    expect(r.parsed.content).toBe("hi");
  });

  it("识别 [DONE] 结束标记", () => {
    const r = parseSSEDataLine("data: [DONE]");
    expect(r.kind).toBe("done");
  });

  it("error 事件进入错误分支（不得被吞为 malformed）", () => {
    const r = parseSSEDataLine('data: {"error":"模型配额耗尽"}');
    expect(r.kind).toBe("error");
    if (r.kind !== "error") return;
    expect(r.message).toBe("模型配额耗尽");
  });

  it("非 data 行（注释/心跳/空行）跳过", () => {
    expect(parseSSEDataLine(": keepalive").kind).toBe("skip");
    expect(parseSSEDataLine("event: ping").kind).toBe("skip");
    expect(parseSSEDataLine("").kind).toBe("skip");
  });

  it("data 前缀无空格的行按严格协议跳过", () => {
    // SSE 协议要求 "data: "；"data:{}" 不合规 → 与 App.tsx 原行为一致：跳过
    expect(parseSSEDataLine("data:{}").kind).toBe("skip");
  });

  it("畸形 JSON 跳过该行（不再抛错断流）", () => {
    // 一行坏数据只该是噪声：此前会冒泡出整个流，并把已产生的回答覆盖成 ❌
    expect(parseSSEDataLine("data: {not json").kind).toBe("skip");
    expect(parseSSEDataLine('data: {"content": "半截').kind).toBe("skip");
  });

  it("error 为空值哨兵（0/false/空串）不算错误", () => {
    expect(parseSSEDataLine("data: {\"error\":0}").kind).toBe("data");
    expect(parseSSEDataLine("data: {\"error\":false}").kind).toBe("data");
    expect(parseSSEDataLine("data: {\"error\":\"\"}").kind).toBe("data");
  });

  it("error 字段为非字符串也归一为字符串", () => {
    const r = parseSSEDataLine('data: {"error":123}');
    expect(r.kind).toBe("error");
    if (r.kind !== "error") return;
    expect(r.message).toBe("123");
  });
});
