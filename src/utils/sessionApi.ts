/**
 * 会话持久化的后端客户端（2026-09-23，审查⑩）。
 *
 * 后端是权威存储（sidecar/sessions.py）：列表只回元数据 + 服务端算好的预览，
 * 消息按 id 按需取；写入是"整会话快照"（前端防抖后 PUT）。
 * 每个函数都返回 null/false 表示**后端不可用**——调用方据此保留 localStorage
 * 这条降级路径，绝不因为后端故障丢掉会话。
 */
import { sidecarFetch } from "./api";

export interface RemoteSessionMeta {
  id: string;
  name: string;
  selectedModel: string;
  lastActive: number;
  updated_at?: string;
  message_count?: number;
  preview?: string;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyMessage = Record<string, any>;

// 传输选择：用 authFetch（Tauri HTTP 插件）——聊天走的就是它，是本应用里
// 验证最充分的路径。（⑩ 调试记录：首版把失败归因于"插件挂住"，实测是**启动竞态**
// ——webview 挂载后 ~1s 就发请求，而 sidecar 还在启动（Python+FastAPI 要 2~4s），
// 连接被拒 → 两条路都报 reqwest 的 "error sending request"。所以列表要带重试。）
async function _call(path: string, method: "GET" | "POST" | "DELETE" = "GET",
                     body?: unknown, timeoutMs = 8000): Promise<Record<string, unknown> | null> {
  try {
    const { authFetch } = await import("./api");
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const resp = await authFetch(path, {
        method,
        signal: ctrl.signal,
        ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
      });
      const text = await resp.text();
      try {
        return JSON.parse(text) as Record<string, unknown>;
      } catch {
        return null;
      }
    } finally {
      clearTimeout(timer);
    }
  } catch {
    return null;
  }
}

/** 等 sidecar 就绪：启动竞态下重试（最多 ~12s），仍然失败才放弃。 */
async function _callRetry(path: string, attempts = 8, delayMs = 700): Promise<Record<string, unknown> | null> {
  for (let i = 0; i < attempts; i++) {
    const data = await _call(path);
    if (data) return data;
    await new Promise((r) => setTimeout(r, delayMs));
  }
  return null;
}

export async function fetchSessionList(): Promise<RemoteSessionMeta[] | null> {
  try {
    const data = await _callRetry("/v1/sessions");
    if (!data || data.status !== "ok") return null;
    return (data.sessions as RemoteSessionMeta[]) || [];
  } catch {
    return null;
  }
}

export async function fetchSessionMessages(id: string): Promise<AnyMessage[] | null> {
  try {
    const data = await _call(`/v1/sessions/${encodeURIComponent(id)}`);
    if (!data || data.status !== "ok") return null;
    return (data.messages as AnyMessage[]) || [];
  } catch {
    return null;
  }
}

export async function putSession(snapshot: {
  id: string;
  name: string;
  selectedModel: string;
  lastActive: number;
  messages: AnyMessage[];
}): Promise<boolean> {
  try {
    const data = await _call(`/v1/sessions/${encodeURIComponent(snapshot.id)}`, "POST", snapshot);
    return !!data && data.status === "ok";
  } catch {
    return false;
  }
}

export async function deleteSessionRemote(id: string): Promise<boolean> {
  try {
    const data = await _call(`/v1/sessions/${encodeURIComponent(id)}`, "DELETE");
    return !!data && data.status === "ok";
  } catch {
    return false;
  }
}

export async function importSessions(
  sessions: Array<{ id: string; name: string; selectedModel?: string; lastActive?: number; messages: AnyMessage[] }>,
): Promise<{ imported: number; skipped: number; total: number } | null> {
  try {
    const data = await _call("/v1/sessions/import", "POST", { sessions });
    if (!data || data.status !== "ok") return null;
    return {
      imported: Number(data.imported || 0),
      skipped: Number(data.skipped || 0),
      total: Number(data.total || 0),
    };
  } catch {
    return null;
  }
}

/** 把会话同步的异常/结果报到 sidecar 日志（走 IPC，不依赖 HTTP 插件）。
 *  静默降级过一次（迁移没发生、日志无痕）——用户数据的事必须留痕。 */
export async function reportSessionIssue(message: string, level: "info" | "warn" = "warn"): Promise<void> {
  try {
    await sidecarFetch("/v1/sessions/report", "POST", { message, level });
  } catch {
    /* 上报失败就算了，不能因为它再抛错 */
  }
}
