/** SSE 行解析（审计 P1-14：流式协议层收窄为纯函数，前端可测）。 */

/** sidecar SSE 事件载荷（字段随 event 变化，可选字段收窄） */
export interface SSEEvent {
  event?: string;
  content?: string;
  reasoning?: string;
  error?: string | number | boolean;
  call_id?: string;
  tool?: string;
  args?: Record<string, unknown>;
  result?: unknown;
  ts?: number;
  engine?: string;
  declared_model?: string;
  is_local?: boolean;
  message?: string;
  iteration?: number;
  [key: string]: unknown;
}

export type SSELineResult =
  | { kind: "skip" }                       // 非 data 行（注释/空行等）
  | { kind: "done" }                       // data: [DONE]
  | { kind: "error"; message: string }     // 事件内 error 字段
  | { kind: "data"; parsed: SSEEvent };

/** 解析单行 SSE。**不抛错**——畸形行（截断/半包/非 JSON）一律按"跳过该行"处理。
 *
 * 此前 JSON.parse 裸奔、调用点在容错 try 之外，一行坏数据就会冒泡出整个流，
 * 并把已经产生的回答覆盖成 ❌（2026-09-24 复查发现）；流式协议里坏行只是噪声，
 * 跳过即可，不该让整轮回话陪葬。 */
export function parseSSEDataLine(line: string): SSELineResult {
  if (!line.startsWith("data: ")) return { kind: "skip" };
  const data = line.substring(6).trim();
  if (data === "[DONE]") return { kind: "done" };
  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    return { kind: "skip" };
  }
  if (!parsed || typeof parsed !== "object") return { kind: "data", parsed: {} };
  const obj = parsed as SSEEvent;
  // 只把"真的有错误"当错误：`error: 0 / false / ""` 是空值哨兵，
  // 误判会让前端 throw 掉整轮（此前条件把 0 和 false 也算错误）。
  const err = obj.error;
  if (err !== undefined && err !== null && err !== "" && err !== 0 && err !== false) {
    return { kind: "error", message: String(err) };
  }
  return { kind: "data", parsed: obj };
}
