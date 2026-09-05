/** SSE 行解析（审计 P1-14：流式协议层收窄为纯函数，前端可测）。 */
export type SSELineResult =
  | { kind: "skip" }                       // 非 data 行（注释/空行等）
  | { kind: "done" }                       // data: [DONE]
  | { kind: "error"; message: string }     // 事件内 error 字段
  | { kind: "data"; parsed: any };

/** 解析单行 SSE。JSON 解析失败抛错（由调用方处理，不吞）。 */
export function parseSSEDataLine(line: string): SSELineResult {
  if (!line.startsWith("data: ")) return { kind: "skip" };
  const data = line.substring(6).trim();
  if (data === "[DONE]") return { kind: "done" };
  const parsed = JSON.parse(data) as any;
  if (!parsed || typeof parsed !== "object") return { kind: "data", parsed: {} };
  const error = parsed.error;
  if (error) return { kind: "error", message: String(error) };
  return { kind: "data", parsed };
}
