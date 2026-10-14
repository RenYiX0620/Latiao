import { useCallback, useEffect, useRef, useState } from "react";
import type { SessionInfo, Message } from "../types";
import { sanitizeSessions } from "../utils/storage";
import {
  deleteSessionRemote, fetchSessionList, fetchSessionMessages, importSessions, putSession,
  reportSessionIssue,
} from "../utils/sessionApi";
import { planSync, sessionFingerprint } from "../utils/sessionSync";

const newSession = (): SessionInfo => ({
  // crypto.randomUUID()：熵充足。此前 Math.random().toString(36).substring(7)
  // 熵仅约 20-30 bit，setMessages 按 id 精确定位，碰撞即跨会话串话（P2）。
  id: `session_${crypto.randomUUID()}`,
  name: "session.default",
  messages: [],
  selectedModel: "",
  lastActive: Date.now(),
});

/** 读本地会话快照（初始渲染与首次迁移共用）。返回 null 表示没有可用数据。 */
function readLocalSessions(): SessionInfo[] | null {
  try {
    const saved = localStorage.getItem("local_ai_os_sessions");
    if (!saved) return null;
    const parsed = sanitizeSessions(saved, () => `msg_${crypto.randomUUID()}`);
    return parsed ? (parsed as unknown as SessionInfo[]) : null;
  } catch {
    return null;
  }
}


/** 启动时最多预载多少个会话的消息（其余懒加载）。见下方"影子副本不能被写空"注释。 */
const EAGER_LOAD_LIMIT = 200;

export function useSessions() {
  const [sessions, setSessions] = useState<SessionInfo[]>(
    () => readLocalSessions() || [newSession()]);
  
  const [currentIdx, setCurrentIdx] = useState(0);

  // Clamp into a valid range — an out-of-range currentIdx must not produce a
  // phantom session (messages written to it would silently evaporate).
  const clampedIdx = sessions.length > 0 ? Math.min(Math.max(currentIdx, 0), sessions.length - 1) : -1;
  const session = clampedIdx >= 0 ? sessions[clampedIdx] : newSession();
  const messages = session.messages;

  const updateSession = (patch: Partial<SessionInfo>) =>
    setSessions((prev) => {
      if (prev.length === 0) return [{ ...newSession(), ...patch }];
      const idx = Math.min(Math.max(currentIdx, 0), prev.length - 1);
      return prev.map((s, i) => (i === idx ? { ...s, ...patch } : s));
    });
  const setSelectedModel = (m: string) => updateSession({ selectedModel: m });
  const deleteSession = (idx: number) => {
    setSessions((prev) => {
      const next = prev.filter((_, i) => i !== idx);
      if (next.length === 0) return [newSession()];
      return next;
    });
    if (currentIdx >= idx) setCurrentIdx((c) => Math.max(0, c - 1));
  };
  // 写入目标：显式会话 id（流式回合在发起时固化，见 setMessagesFor）或
  // "当前正在查看的会话"（普通交互）。
  const _applyMessages = (explicitId: string | null, fn: (prev: Message[]) => Message[]) => {
    setSessions((prev) => {
      // If the list is somehow empty, materialize a real session so the new
      // messages live inside `sessions` instead of a throwaway fallback.
      if (prev.length === 0) return [{ ...newSession(), messages: fn([]) }];
      // 按会话 id 定位，而非闭包捕获的 currentIdx：流式进行中列表头部
      // 插入/删除会话（cron 心跳、用户删会话）会使索引指向别的会话，
      // 内容串话。id 是稳定标识，插入删除不影响定位。
      const idx = Math.min(Math.max(currentIdx, 0), prev.length - 1);
      // 09-23：显式 id 优先——流式回合的目标在发起时固化，用户中途切到别的
      // 会话（或删掉它）都不会把这一轮流的内容写进别的会话。
      const targetId = explicitId || prev[idx]?.id;
      return prev.map((s, i) => {
        if (targetId ? s.id !== targetId : i !== idx) return s;
        const newMsgs = fn(s.messages);
        let name = s.name;
        if (s.name === "session.default" && newMsgs.length > 0) {
          const firstUser = newMsgs.find((m: Message) => m.role === "user");
          if (firstUser?.content) name = firstUser.content.slice(0, 20).replace(/\n/g, " ") + (firstUser.content.length > 20 ? "…" : "");
        }
        return { ...s, messages: newMsgs, name, lastActive: Date.now() };
      });
    });
  };
  const setMessages = (fn: (prev: Message[]) => Message[]) => _applyMessages(null, fn);
  /** 按**指定会话 id** 写入：流式回合发起时固化目标，中途切会话也不串话。 */
  const setMessagesFor = (sessionId: string, fn: (prev: Message[]) => Message[]) =>
    _applyMessages(sessionId, fn);

  // ── 后端持久化（2026-09-23，审查⑩）────────────────────────────────
  // 三层责任：①启动时从后端取列表（拿不到就留在 localStorage，绝不丢会话）；
  // ②后端为空且本地有内容 → 一次性导入（幂等）；③变更按会话快照同步回后端。
  // localStorage 仍照常写（App 里那条 effect）——它是降级路径，不是主存储。
  const _syncedRef = useRef<Record<string, string>>({});   // id → 上次同步的内容指纹
  const _backendIdsRef = useRef<Set<string>>(new Set());   // 后端已有的会话 id（删除判定用）
  const _pendingRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const [backendReady, setBackendReady] = useState(false);

  // 变更同步：按会话 diff（内容指纹变了才 PUT），删除的会话 DELETE。
  // 稳定引用（useCallback [] + ref 读 backendReady）：否则 App 落盘 effect 依赖它
  // 每渲染重跑 → 不在流式时每敲一键就写 localStorage（P0 性能）。
  const _backendReadyRef = useRef(backendReady);
  useEffect(() => { _backendReadyRef.current = backendReady; }, [backendReady]);
  const syncRemote = useCallback((list: SessionInfo[]) => {
    if (!_backendReadyRef.current) return;
    const plan = planSync(_syncedRef.current, list, _backendIdsRef.current);
    _syncedRef.current = plan.nextFingerprints;
    for (const id of plan.puts) {
      const s0 = list.find((x) => x.id === id);
      if (!s0) continue;
      const payload = {
        id: s0.id, name: s0.name, selectedModel: s0.selectedModel || "",
        lastActive: s0.lastActive || 0, messages: s0.messages,
      };
      const timer = _pendingRef.current[id];
      if (timer) clearTimeout(timer);
      // 每会话独立防抖：流式期间内容一直变，避免每个 token 都发一次
      _pendingRef.current[id] = setTimeout(() => {
        delete _pendingRef.current[id];
        void putSession(payload);
      }, 1200);
    }
    for (const id of plan.deletes) {
      _backendIdsRef.current.delete(id);
      void deleteSessionRemote(id);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
      let list = await fetchSessionList();
      if (cancelled) return;
      if (list === null) {
        console.warn("[sessions] 后端不可用，继续使用 localStorage（降级）");
        void reportSessionIssue("读取后端会话列表失败 → 降级用 localStorage");
        return;
      }
      // 迁移：不是"后端空才导"，而是**按会话补**（后端缺哪个就导哪个）。
      // 首版用 list.length===0 当开关，于是后端只要有一条别的记录（哪怕是个空
      // 会话），本地既有会话就永远进不去——真机上正是这么卡住的。
      const backendIds = new Set(list.map((m) => m.id));
      const local = (readLocalSessions() || []).filter((s) => s.messages.length > 0);
      const toImport = local.filter((s) => !backendIds.has(s.id));
      let _rawLen = -1;
      let _parsed = 0;
      try {
        const raw = localStorage.getItem("local_ai_os_sessions");
        _rawLen = raw ? raw.length : 0;
        _parsed = raw ? (JSON.parse(raw) as unknown[]).length : 0;
      } catch { /* ignore */ }
      if (toImport.length) {
        const r = await importSessions(toImport);
        void reportSessionIssue(
          r ? `迁移本地会话：新增 ${r.imported} / 跳过 ${r.skipped} / 共 ${r.total}` +
              `（本地 raw=${_rawLen} 字符 / 解析 ${_parsed} 个 / 待迁 ${toImport.length}）`
            : `迁移失败（后端不可用；本地 raw=${_rawLen} / 解析 ${_parsed} / 待迁 ${toImport.length}）`,
          r ? "info" : "warn");
        if (r && r.imported > 0) {
          const refreshed = await fetchSessionList();
          if (refreshed) list = refreshed;      // 导入后重取，列表里就能看到本地会话
        }
      } else if (_parsed > 0) {
        void reportSessionIssue(`本地会话均已存在后端（本地 ${_parsed} 个）`, "info");
      }
      if (list.length === 0) {
        // 后端与本地都没有 → 保持新会话即可
        local.forEach((s0) => {
          _syncedRef.current[s0.id] = sessionFingerprint({ ...s0, loaded: true });
          _backendIdsRef.current.add(s0.id);
        });
        setBackendReady(true);
        return;
      }
      // 后端有数据 → 用后端列表（元数据 + 预览），消息懒加载
      const metas: SessionInfo[] = list.map((m) => ({
        id: m.id,
        name: m.name || "session.default",
        selectedModel: m.selectedModel || "",
        lastActive: m.lastActive || 0,
        messages: [],
        preview: m.preview || "",
        message_count: m.message_count || 0,
        loaded: false,
      }));
      if (cancelled) return;
      metas.forEach((m) => _backendIdsRef.current.add(m.id));
      // 未载入的会话不参与同步（否则空消息会覆盖后端）——由 planSync 保证；
      // 标记 backendReady 之前先把 id 登记好
      setSessions(metas);
      setCurrentIdx(0);
      // 启动时把消息**全部**载入（上限 EAGER_LOAD_LIMIT）：
      // 为什么不用懒加载——localStorage 影子副本是按内存状态写的，未载入的会话
      // 消息为空 → 影子会被"写空"（实测：187 条变成 6 条）。影子是后端不可用时的
      // 唯一退路，不能被自己的写入掏空。超出上限的老会话仍走懒加载（切换时取）。
      const eager = metas.slice(0, EAGER_LOAD_LIMIT);
      const rest = metas.slice(EAGER_LOAD_LIMIT);
      const loadedMetas = await Promise.all(eager.map(async (m) => {
        const msgs = await fetchSessionMessages(m.id);
        return { id: m.id, msgs };
      }));
      if (!cancelled) {
        const byId = new Map(loadedMetas.map((x) => [x.id, x.msgs]));
        setSessions((prev) => prev.map((s) => {
          const msgs = byId.get(s.id);
          if (msgs) {
            _syncedRef.current[s.id] = sessionFingerprint(
              { ...s, messages: msgs, loaded: true });
            return { ...s, messages: msgs as unknown as Message[], loaded: true };
          }
          if (rest.some((r) => r.id === s.id)) return s;      // 超出上限：保持未载入
          return s;
        }));
      }
      setBackendReady(true);
      } catch (e) {
        // 初始化失败不该让应用起不来：降级用 localStorage（严格说上面每个网络调用
        // 都各自兜了错，这里是最后一道）。
        console.warn("[sessions] 初始化异常，继续用本地存储", e);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const switchSessionWithLoad = (idx: number) => {
    setCurrentIdx(idx);
    const target = sessions[Math.min(Math.max(idx, 0), sessions.length - 1)];
    if (!target || target.loaded || !backendReady) return;
    void (async () => {
      const msgs = await fetchSessionMessages(target.id);
      if (!msgs) return;
      setSessions((prev) => prev.map((s) => (s.id === target.id
        ? { ...s, messages: msgs as unknown as Message[], loaded: true } : s)));
      _syncedRef.current[target.id] = sessionFingerprint(
        { ...target, messages: msgs, loaded: true });
    })();
  };

  return {
    sessions, setSessions,
    backendReady, syncRemote,
    currentIdx, setCurrentIdx,
    session, messages,
    updateSession, setSelectedModel,
    switchSession: switchSessionWithLoad, deleteSession, setMessages, setMessagesFor,
    newSession,
  };
}
