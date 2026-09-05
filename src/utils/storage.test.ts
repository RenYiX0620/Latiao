import { describe, it, expect } from "vitest";
import { sanitizeSessions, saveSessionsWithFallback, type KVStorage } from "./storage";

const genId = () => `msg_${Math.random().toString(36).slice(2)}`;

/** 内存 KV，支持注入 setItem 失败次数以模拟配额超限。 */
function memKV(failTimes: number): KVStorage & { calls: string[] } {
  let fails = failTimes;
  const store = new Map<string, string>();
  return {
    calls: [],
    setItem(k, v) {
      this.calls.push(v);
      if (fails > 0) {
        fails--;
        throw new Error("QuotaExceededError");
      }
      store.set(k, v);
    },
    removeItem(k) {
      store.delete(k);
    },
  };
}

describe("sanitizeSessions（会话快照校验）", () => {
  it("空/无值返回 null", () => {
    expect(sanitizeSessions(null, genId)).toBeNull();
    expect(sanitizeSessions("", genId)).toBeNull();
  });

  it("坏 JSON / 非数组 / 空数组返回 null（防整屏崩）", () => {
    expect(sanitizeSessions("{bad", genId)).toBeNull();
    expect(sanitizeSessions('{"a":1}', genId)).toBeNull();
    expect(sanitizeSessions("[]", genId)).toBeNull();
  });

  it("补 lastActive 与消息 id（缺 id 用熵源生成）", () => {
    const raw = JSON.stringify([
      { id: "s1", messages: [{ role: "user", content: "hi" }] },
    ]);
    const out = sanitizeSessions(raw, genId);
    expect(out).not.toBeNull();
    expect(out![0].lastActive).toBeGreaterThan(0);
    expect(out![0].messages![0].id).toMatch(/^msg_/);
  });

  it("已有 id 的消息不重写", () => {
    const raw = JSON.stringify([
      { id: "s1", lastActive: 123, messages: [{ id: "keep_me", role: "user" }] },
    ]);
    const out = sanitizeSessions(raw, genId)!;
    expect(out[0].messages![0].id).toBe("keep_me");
    expect(out[0].lastActive).toBe(123);
  });
});

describe("saveSessionsWithFallback（P0-5 配额降级）", () => {
  const bigSession = (n: number, m: number) => {
    const sess = { id: `s${n}`, messages: Array.from({ length: m }, (_, i) => ({ id: `m${n}-${i}`, role: "user", content: "x", imageBase64: "ABCD" })) };
    return sess;
  };

  it("正常保存一次成功", () => {
    const kv = memKV(0);
    const ok = saveSessionsWithFallback(JSON.stringify([bigSession(1, 3)]), kv);
    expect(ok).toBe(true);
    expect(kv.calls.length).toBe(1);
  });

  it("配额超限 → L1 剥图片后成功", () => {
    const kv = memKV(1);
    const ok = saveSessionsWithFallback(JSON.stringify([bigSession(1, 3)]), kv);
    expect(ok).toBe(true);
    expect(kv.calls.length).toBe(2);
    const saved = JSON.parse(kv.calls[1]);
    expect(saved[0].messages[0].imageBase64).toBeUndefined();
  });

  it("配额超限两次 → L2 保留最近 2 会话", () => {
    const kv = memKV(2);
    const ok = saveSessionsWithFallback(JSON.stringify([bigSession(1, 2), bigSession(2, 2), bigSession(3, 2)]), kv);
    expect(ok).toBe(true);
    const saved = JSON.parse(kv.calls[kv.calls.length - 1]);
    expect(saved.length).toBe(2);
    expect(saved[0].id).toBe("s2");
    expect(saved[1].id).toBe("s3");
  });

  it("配额超限两次 → L3 保最近 1 会话 20 条（L2 需 >=2 会话，单会话时跳过）", () => {
    const kv = memKV(2);
    const ok = saveSessionsWithFallback(JSON.stringify([bigSession(1, 40)]), kv);
    expect(ok).toBe(true);
    const saved = JSON.parse(kv.calls[kv.calls.length - 1]);
    expect(saved.length).toBe(1);
    expect(saved[0].messages.length).toBe(20);
  });

  it("一直失败 → 清空后保底 10 条（不静默全丢）", () => {
    const kv = memKV(99);
    const ok = saveSessionsWithFallback(JSON.stringify([bigSession(1, 30)]), kv);
    expect(ok).toBe(true); // 保底路径最终返回 true
    expect(kv.calls[kv.calls.length - 1]).toBeTruthy();
  });

  it("坏数据返回 false（调用方放弃持久化，不抛）", () => {
    const kv = memKV(1); // 首次写失败 → 进入解析 → 坏 JSON → false
    expect(saveSessionsWithFallback("{bad", kv)).toBe(false);
  });
});
