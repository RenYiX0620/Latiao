/** 会话本地存储的校验与配额降级（审计 P0-5 / P1-14：纯函数化后前端可测）。 */

export interface SessionLike {
  lastActive?: number;
  messages?: Array<{ id?: string; imageBase64?: string; imagePreview?: string } & Record<string, unknown>>;
  [key: string]: unknown;
}

const STORAGE_KEY = "local_ai_os_sessions";

export interface KVStorage {
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export function defaultStorage(): KVStorage | null {
  return typeof localStorage !== "undefined" ? (localStorage as KVStorage) : null;
}

/** 解析并校验会话快照：非 JSON / 非数组 / 空数返回 null；补 lastActive 与消息 id（P2-30 同款熵源）。 */
export function sanitizeSessions(
  raw: string | null | undefined,
  genId: () => string,
): SessionLike[] | null {
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.length === 0) return null;
    return parsed.map((s) => {
      const sess = (s || {}) as SessionLike;
      return {
        ...sess,
        lastActive: sess.lastActive || Date.now(),
        messages: (sess.messages || []).map((m) => ({
          ...m,
          id: m.id || genId(),
        })),
      };
    });
  } catch {
    return null;
  }
}

/**
 * 配额超限降级保存（P0-5 修复的纯函数版）：
 * L0 原样 → L1 剥图片 → L2 最近 2 会话 → L3 最近 1 会话 20 条 → 清空后保底 10 条。
 *
 * **数组顺序契约（2026-10-11 修复）**：会话数组是**最新在前**（App.tsx 新建会话用
 * `[ns, ...prev]`）。此函数此前用 `slice(-2)` / `[length-1]` 取"尾部"，实际拿到的是
 * **最旧的**几个——quota 紧张时正好丢掉刚聊的会话，而注释写的是"最近"。现在按
 * 索引 0 取最新；测试同步修正（旧测试把错误语义固化住了）。
 */
export function saveSessionsWithFallback(raw: string, storage: KVStorage | null = defaultStorage()): boolean {
  const trySave = (s: string): boolean => {
    try {
      storage?.setItem(STORAGE_KEY, s);
      return true;
    } catch {
      return false;
    }
  };
  if (trySave(raw)) return true;
  if (!storage) return false;
  try {
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr)) return false;
    // L1：剥全部图片字段（stripForStorage 已做，此处兜底）
    const noImg = arr.map((s: SessionLike) => ({
      ...s,
      messages: (s.messages || []).map((m) => ({
        ...m,
        imageBase64: undefined,
        imagePreview: undefined,
      })),
    }));
    if (trySave(JSON.stringify(noImg))) return true;
    // L2：只保留最近 2 个会话（索引 0 = 最新）
    if (arr.length > 1 && trySave(JSON.stringify(noImg.slice(0, 2)))) return true;
    // L3：最近 1 个会话的最后 20 条
    const newest = noImg[0];
    if (newest && trySave(JSON.stringify([{ ...newest, messages: (newest.messages || []).slice(-20) }]))) return true;
    // 保底：清空旧数据后保存本次会话最后 10 条
    try {
      storage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
    if (newest) trySave(JSON.stringify([{ ...newest, messages: (newest.messages || []).slice(-10) }]));
    return true;
  } catch {
    return false;
  }
}
