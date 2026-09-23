/**
 * 会话同步的**决策逻辑**（2026-09-23，审查⑩）——纯函数，便于测试。
 *
 * 抽出来的原因：这里是唯一可能丢数据的地方。要守两条：
 * ① 消息还没从后端载入（loaded === false）的会话**绝不**参与同步——
 *    否则"列表刚回来、消息还是空数组"会被判定成内容变化，用空消息覆盖后端。
 * ② 内容没变就不发（指纹比对），流式期间每会话独立防抖由调用方负责。
 */

export interface SyncableSession {
  id: string;
  name: string;
  selectedModel?: string;
  lastActive?: number;
  messages: unknown[];
  loaded?: boolean;
}

export interface SyncPlan {
  /** 需要 PUT 的会话 id（内容有变化且可同步） */
  puts: string[];
  /** 需要 DELETE 的会话 id（本地已不存在） */
  deletes: string[];
  /** 更新后的指纹表（调用方保存下来，供下次比对） */
  nextFingerprints: Record<string, string>;
}

export function sessionFingerprint(s: SyncableSession): string {
  return JSON.stringify({
    id: s.id,
    name: s.name,
    selectedModel: s.selectedModel || "",
    lastActive: s.lastActive || 0,
    messages: s.messages,
  });
}

export function planSync(
  fingerprints: Record<string, string>,
  sessions: SyncableSession[],
  alreadyOnBackend: Set<string>,
): SyncPlan {
  const puts: string[] = [];
  const next: Record<string, string> = {};
  const seen = new Set<string>();
  for (const s of sessions) {
    seen.add(s.id);
    // ① 消息未载入 → 既不同步也不记指纹（等载入后再说）
    if (s.loaded === false) {
      if (fingerprints[s.id]) next[s.id] = fingerprints[s.id];
      continue;
    }
    const fp = sessionFingerprint(s);
    next[s.id] = fp;
    if (fingerprints[s.id] !== fp) puts.push(s.id);
  }
  const deletes: string[] = [];
  for (const id of Object.keys(fingerprints)) {
    if (!seen.has(id) && alreadyOnBackend.has(id)) deletes.push(id);
  }
  return { puts, deletes, nextFingerprints: next };
}
