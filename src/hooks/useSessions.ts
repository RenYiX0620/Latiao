import { useState } from "react";
import type { SessionInfo, Message } from "../types";

const newSession = (): SessionInfo => ({
  id: `session_${Math.random().toString(36).substring(7)}`,
  name: "session.default",
  messages: [],
  selectedModel: "",
  lastActive: Date.now(),
});

export function useSessions() {
  const [sessions, setSessions] = useState<SessionInfo[]>(() => {
    try {
      const saved = localStorage.getItem("local_ai_os_sessions");
      if (saved) { const parsed = JSON.parse(saved); if (Array.isArray(parsed) && parsed.length > 0) return parsed.map((s: any) => ({ ...s, lastActive: s.lastActive || Date.now(), messages: (s.messages || []).map((m: Message) => ({ ...m, id: m.id || `msg_${Math.random().toString(36).slice(2)}` })) })); }
    } catch { /* ignore */ }
    return [newSession()];
  });
  
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
  const switchSession = (idx: number) => { setCurrentIdx(idx); };
  const deleteSession = (idx: number) => {
    setSessions((prev) => {
      const next = prev.filter((_, i) => i !== idx);
      if (next.length === 0) return [newSession()];
      return next;
    });
    if (currentIdx >= idx) setCurrentIdx((c) => Math.max(0, c - 1));
  };
  const setMessages = (fn: (prev: Message[]) => Message[]) => {
    setSessions((prev) => {
      // If the list is somehow empty, materialize a real session so the new
      // messages live inside `sessions` instead of a throwaway fallback.
      if (prev.length === 0) return [{ ...newSession(), messages: fn([]) }];
      const idx = Math.min(Math.max(currentIdx, 0), prev.length - 1);
      return prev.map((s, i) => {
        if (i !== idx) return s;
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

  return {
    sessions, setSessions,
    currentIdx, setCurrentIdx,
    session, messages,
    updateSession, setSelectedModel,
    switchSession, deleteSession, setMessages,
    newSession,
  };
}
