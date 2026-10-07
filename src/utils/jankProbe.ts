/**
 * 主线程卡顿诊断探针（2026-10-06，常驻诊断能力）。
 *
 * 背景：输入打字卡（拼音上屏一个一个蹦），静态勘查定位到四个嫌疑
 * （流式 flush 0ms/Markdown 全量重解析/心跳 setState 风暴/合成层 blur），
 * 但缺运行时证据定罪。本探针把两类硬数据送到侧车日志：
 *   1. longtask：>50ms 的主线程阻塞（PerformanceObserver）
 *   2. 慢渲染：ChatView 单次渲染 >40ms（由 reportRender 上报，附消息数）
 *
 * 输出：POST /v1/diag/trace → sidecar logger → sidecar.log 的 [diag] 行。
 * 上报全部节流（汇总每 30s 一批、慢渲染每 5s 至多 2 条），无隐私内容（纯数字）。
 */
const SIDECAR = "http://127.0.0.1:8765";

let enabled = false;
let longtasks = 0;
let longtaskMs = 0;
let longtaskWorst = 0;
let pending: string[] = [];
let lastPost = 0;

async function post(lines: string[]) {
  try {
    const { getToken } = await import("./api");
    const token = await getToken();
    await fetch(`${SIDECAR}/v1/diag/trace`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(token ? { "X-Latiao-Token": token } : {}) },
      body: JSON.stringify({ source: "jank", lines }),
      signal: AbortSignal.timeout(3000),
    });
  } catch { /* 侧车未就绪：丢弃本轮，不重试（诊断数据可牺牲） */ }
}

function flush(force = false) {
  const now = Date.now();
  const batch: string[] = [];
  if (longtasks > 0) {
    batch.push(`longtask ×${longtasks} 共${Math.round(longtaskMs)}ms 最长${Math.round(longtaskWorst)}ms（30s 窗口）`);
    longtasks = 0; longtaskMs = 0; longtaskWorst = 0;
  }
  batch.push(...pending);
  pending = [];
  if (batch.length === 0) return;
  if (!force && now - lastPost < 5000) { pending = batch; return; }  // 限流：≥5s 一批
  lastPost = now;
  void post(batch);
}

/** 启动探针（App 挂载时调用一次）。 */
export function initJankProbe() {
  if (enabled) return;
  enabled = true;
  try {
    const obs = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        longtasks += 1;
        longtaskMs += e.duration;
        longtaskWorst = Math.max(longtaskWorst, e.duration);
      }
    });
    obs.observe({ entryTypes: ["longtask"] });
    setInterval(() => flush(), 30_000);
  } catch { /* 环境不支持：静默退出 */ }
}

/** ChatView 渲染埋点：单次渲染耗时超过阈值才上报（节流防刷屏）。 */
export function reportSlowRender(durationMs: number, messageCount: number) {
  if (!enabled || durationMs < 40) return;
  pending.push(`渲染 ${Math.round(durationMs)}ms · ${messageCount} 条消息`);
  flush(true);
}
