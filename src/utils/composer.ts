/**
 * 输入框回车判定的纯逻辑（抽出来便于单测）。
 *
 * 背景：WKWebView 下按 Enter 确认输入法候选词时，`compositionend` 会先于最终
 * keydown 派发，此时 `e.nativeEvent.isComposing` 已经是 false —— 若只看这个标记，
 * 半句话就会被发出去（中文输入法高频误发送）。所以用 compositionend 的时间戳留
 * 一小段缓冲，专门盖住紧跟其后的那一次 keydown。
 *
 * 09-21 修的两个坑（都是这段缓冲的副作用，"回车只换行、点按钮才发送"）：
 *  ① 组词开始时把缓冲设成 `Number.MAX_SAFE_INTEGER` 是个**单向闩锁**：一旦某次
 *     `compositionend` 没派发（WKWebView 组词事件本就不稳、或组词中途失焦/取消），
 *     这个值再也不会被复位，`Date.now() < until` 永远为真 → 该会话里回车永远只
 *     换行。改成有界兜底：事件丢了最多损失几秒。
 *  ② 缓冲 300ms 太长：你打完字、组词刚落，紧接着按回车想发送也会被吞掉。
 *     确认候选的那一次 keydown 与 compositionend 在同一轮事件里（微秒级），
 *     所以 60ms 足够。
 *  另外，守卫命中时调用方不要 `preventDefault()`，让 textarea 的默认换行照常发生
 *  —— 这也正是"回车变成换行"这个可见现象的来源。
 */

/** 组词结束后的吞键窗口：只盖住 compositionend 之后紧跟的那一次 keydown。 */
export const COMPOSE_GRACE_MS = 60;

/** 组词开始时的兜底上限：compositionend 丢失时不至于把回车永久锁死。 */
export const COMPOSING_BACKSTOP_MS = 3000;

/** 输入法处理中的标准 keyCode（Enter 确认候选词时常见）。 */
export const IME_PROCESSING_KEYCODE = 229;

export interface EnterKeyInfo {
  key: string;
  shiftKey: boolean;
  isComposing: boolean;
  keyCode?: number;
}

/** 这次按键是否属于"输入法正在/刚处理完组词"。 */
export function isComposingKey(
  now: number,
  composingUntil: number,
  e: Pick<EnterKeyInfo, "isComposing" | "keyCode">,
): boolean {
  return e.isComposing === true
    || e.keyCode === IME_PROCESSING_KEYCODE
    || now < composingUntil;
}

/** 这次按键是否应当触发发送（Enter、非 Shift、非组词、且不在生成中）。 */
export function shouldSendOnEnter(
  now: number,
  composingUntil: number,
  isProcessing: boolean,
  e: EnterKeyInfo,
): boolean {
  if (isProcessing) return false;
  if (e.key !== "Enter" || e.shiftKey) return false;
  return !isComposingKey(now, composingUntil, e);
}

/** 工具结果回灌给模型时的截断（2026-09-29，与后端 `_truncate_keep_lines` 同一策略）。
 *
 * 后端 compaction 早就按**整行**截断（旧实现 `slice(0,n)` 会把 `1376.55` 切成 `137`，
 * 模型据此复述出错值）；前端这条回灌路径此前仍是 `raw.slice(0, 1000)`——**从行中间切**，
 * 而这是恢复会话里模型看到工具结果的唯一形态。现在同样按行边界切，并写明省略量，
 * 让模型知道"要看全的得重新调用工具"，而不是凭半截数字复述。
 */
export function truncateToolResult(text: string, limit = 1000): string {
  const raw = String(text ?? "");
  if (raw.length <= limit) return raw;
  const lines = raw.split("\n");
  const kept: string[] = [];
  let used = 0;
  for (const ln of lines) {
    if (kept.length && used + ln.length + 1 > limit) break;
    // 单行本身就超限：只留这一行并截断它（后面会加标记说明）
    kept.push(ln.length > limit ? ln.slice(0, limit) : ln);
    used += ln.length + 1;
    if (used >= limit) break;
  }
  const body = kept.join("\n");
  const droppedLines = Math.max(0, lines.length - kept.length);
  const droppedChars = Math.max(0, raw.length - body.length);
  return (
    body +
    `\n…(已省略 ${droppedLines} 行 / ${droppedChars} 字符；需要完整内容请重新调用该工具，不要凭记忆引用具体数字)…`
  );
}
