import { describe, expect, it } from "vitest";

import {
  COMPOSE_GRACE_MS,
  COMPOSING_BACKSTOP_MS,
  IME_PROCESSING_KEYCODE,
  isComposingKey,
  shouldSendOnEnter,
} from "./composer";

const enter = { key: "Enter", shiftKey: false, isComposing: false, keyCode: 13 };
const NOW = 1_700_000_000_000;

describe("isComposingKey", () => {
  it("组词中（isComposing）算组词键", () => {
    expect(isComposingKey(NOW, 0, { isComposing: true })).toBe(true);
  });

  it("keyCode 229（输入法处理中）算组词键", () => {
    expect(isComposingKey(NOW, 0, { isComposing: false, keyCode: IME_PROCESSING_KEYCODE })).toBe(true);
  });

  it("compositionend 之后的缓冲窗口内算组词键", () => {
    expect(isComposingKey(NOW, NOW + COMPOSE_GRACE_MS, { isComposing: false })).toBe(true);
  });

  it("超出缓冲窗口后不再是组词键", () => {
    expect(isComposingKey(NOW, NOW - 1, { isComposing: false })).toBe(false);
  });
});

describe("shouldSendOnEnter", () => {
  it("普通回车 → 发送", () => {
    expect(shouldSendOnEnter(NOW, 0, false, enter)).toBe(true);
  });

  it("Shift+Enter → 换行，不发送", () => {
    expect(shouldSendOnEnter(NOW, 0, false, { ...enter, shiftKey: true })).toBe(false);
  });

  it("生成中 → 不发送", () => {
    expect(shouldSendOnEnter(NOW, 0, true, enter)).toBe(false);
  });

  it("组词中按回车（确认候选词）→ 不发送", () => {
    expect(shouldSendOnEnter(NOW, 0, false, { ...enter, isComposing: true })).toBe(false);
  });

  it("刚组词完的缓冲窗口内 → 不发送（半句话不会被发出去）", () => {
    expect(shouldSendOnEnter(NOW, NOW + COMPOSE_GRACE_MS, false, enter)).toBe(false);
  });

  it("缓冲窗口过后，同一句回车可以发送（回归：300ms 太长导致每次都被吞）", () => {
    expect(shouldSendOnEnter(NOW, NOW - COMPOSE_GRACE_MS - 1, false, enter)).toBe(true);
  });

  it("回归：兜底过期后回车必须能发送（旧实现用 MAX_SAFE_INTEGER 会永久锁死）", () => {
    const latch = NOW + COMPOSING_BACKSTOP_MS;
    expect(shouldSendOnEnter(NOW + COMPOSING_BACKSTOP_MS + 1, latch, false, enter)).toBe(true);
    expect(COMPOSING_BACKSTOP_MS).toBeLessThan(60_000);
  });

  it("其他按键不受影响（如字母键、方向键）", () => {
    expect(shouldSendOnEnter(NOW, 0, false, { ...enter, key: "a" })).toBe(false);
    expect(shouldSendOnEnter(NOW, 0, false, { ...enter, key: "ArrowUp" })).toBe(false);
  });
});
