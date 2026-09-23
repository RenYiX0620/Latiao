/**
 * 从"可能是任何东西"的入参里解析出会话 id（2026-09-23 真机崩溃修复）。
 *
 * 事故：`stopGeneration(targetSession?: string)` 被直接挂在按钮上
 * （`onStop={stopGeneration}` → 子组件 `onClick={onStop}`），React 会把**点击事件
 * 对象**当第一个参数传进来 → 之后 `JSON.stringify({session_id: 事件对象})` 因为
 * 事件里带 DOM 引用（循环结构）直接抛 TypeError，应用崩溃并弹"重新加载"。
 * TypeScript 没能拦住：`(targetSession?: string) => void` 可以赋给 `() => void`
 * （可选参数让它看起来是无参处理器），于是类型检查一路放行。
 *
 * 所以：处理器里对入参做**运行时**收敛——只认非空字符串，其余一律回落到兜底值。
 */
export function resolveStopTarget(target: unknown, fallback: string): string {
  return typeof target === "string" && target.trim() ? target : fallback;
}
