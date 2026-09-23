/**
 * ⑩ 会话同步决策的测试（2026-09-23）。
 *
 * 这里守的是**唯一可能丢数据的地方**：把"列表已回来但消息还没载入"的空数组
 * 当成内容变化发上去，就会用空消息覆盖后端会话。真机首版迁移还踩过另一个坑：
 * 用 setState 回调"读"当前状态，React 判定引用未变时不执行回调 → Promise 永不
 * resolve → 迁移静默不发生（所以改成直接读 localStorage，并有 live 验证）。
 */
import { describe, expect, it } from "vitest";
import { planSync, sessionFingerprint, type SyncableSession } from "./sessionSync";

const sess = (over: Partial<SyncableSession> = {}): SyncableSession => ({
  id: "s1",
  name: "会话",
  selectedModel: "hermes",
  lastActive: 100,
  messages: [{ id: "m1", role: "user", content: "你好" }],
  loaded: true,
  ...over,
});

describe("planSync", () => {
  it("内容没变 → 不发", () => {
    const s = sess();
    const fp = { s1: sessionFingerprint(s) };
    const plan = planSync(fp, [s], new Set(["s1"]));
    expect(plan.puts).toEqual([]);
    expect(plan.deletes).toEqual([]);
  });

  it("内容变了 → 发该会话", () => {
    const s = sess({ messages: [{ id: "m1", role: "user", content: "改了" }] });
    const plan = planSync({ s1: sessionFingerprint(sess()) }, [s], new Set(["s1"]));
    expect(plan.puts).toEqual(["s1"]);
  });

  it("消息未载入（loaded=false）→ 绝不发（否则空消息覆盖后端）", () => {
    const unloaded = sess({ messages: [], loaded: false });
    const plan = planSync({}, [unloaded], new Set(["s1"]));
    expect(plan.puts).toEqual([]);
    expect(plan.nextFingerprints.s1).toBeUndefined();
  });

  it("未载入但之前有指纹 → 保留指纹，等载入后再比", () => {
    const before = sess();
    const fp = { s1: sessionFingerprint(before) };
    const plan = planSync(fp, [sess({ messages: [], loaded: false })], new Set(["s1"]));
    expect(plan.puts).toEqual([]);
    expect(plan.nextFingerprints.s1).toBe(fp.s1);
  });

  it("本地删掉的会话 → 若后端有则删除", () => {
    const plan = planSync({ s1: "x", s2: "y" }, [sess({ id: "s2" })], new Set(["s1", "s2"]));
    expect(plan.deletes).toEqual(["s1"]);
  });

  it("本地删掉但后端也没有 → 不删（避免无谓请求）", () => {
    const plan = planSync({ s1: "x" }, [], new Set<string>());
    expect(plan.deletes).toEqual([]);
  });

  it("新会话（无指纹）→ 发", () => {
    const plan = planSync({}, [sess({ id: "s9" })], new Set());
    expect(plan.puts).toEqual(["s9"]);
  });
});
