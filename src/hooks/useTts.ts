/** TTS 朗读（本地服务 + 系统语音兜底）。从 App.tsx 拆出（2026-09-24）。 */
import { useCallback, useEffect, useRef, useState } from "react";
import { authFetch } from "../utils/api";
import { pickVoice, speechSupported, splitSentences, stripForSpeech } from "../utils/speech";

interface Deps {
  showToast: (msg: string, type?: string) => void;
  t: (key: string, params?: Record<string, string | number>) => string;
  ttsEnabled: boolean;
  ttsRate: number;
  ttsVoice: string;
  ttsTimeoutMs: number;
  ttsLocalVoices: string[];
  ttsLocalVoice: string;
  ttsPitch: number;
  ttsEmotion: string;
  ttsVoices: SpeechSynthesisVoice[];
  lang: string;
  session: { id: string };
}

export function useTts(deps: Deps) {
  const {
    showToast, t, ttsEnabled, ttsRate, ttsVoice, ttsTimeoutMs,
    ttsLocalVoices, ttsLocalVoice, ttsPitch, ttsEmotion, ttsVoices, lang, session,
  } = deps;
  void ttsVoice; // 系统语音当前用默认音色，保留参数供后续
  const speakingIdRef = useRef<string | null>(null);
  const ttsAudioRef = useRef<HTMLAudioElement | null>(null);
  const [speakingId, setSpeakingId] = useState<string | null>(null);

  const stopSpeaking = useCallback(() => {
    try { window.speechSynthesis?.cancel(); } catch { /* ignore */ }
    const audio = ttsAudioRef.current;
    if (audio) {
      try { audio.pause(); } catch { /* ignore */ }
      ttsAudioRef.current = null;
    }
    speakingIdRef.current = null;
    setSpeakingId(null);
  }, []);

  const systemSpeak = useCallback((text: string) => {
    const chunks = splitSentences(text);
    if (!chunks.length) { speakingIdRef.current = null; setSpeakingId(null); return; }
    const voice = pickVoice(ttsVoices, lang);
    chunks.forEach((chunk, idx) => {
      const utter = new SpeechSynthesisUtterance(chunk);
      if (voice) utter.voice = voice;
      utter.rate = ttsRate;
      // 系统语音也支持音高（0~2，默认 1）：本地服务不可用时回退到这里，
      // 音高滑杆不该跟着失效 —— 变调方向与本地路径一致（1.15 = 升 15%）
      utter.pitch = Math.min(2, Math.max(0.1, ttsPitch));
      utter.lang = voice?.lang || lang;
      if (idx === chunks.length - 1) {
        utter.onend = () => {
          if (speakingIdRef.current) { speakingIdRef.current = null; setSpeakingId(null); }
        };
      }
      window.speechSynthesis.speak(utter);
    });
  }, [ttsVoices, lang, ttsRate, ttsPitch]);

  /** 朗读一条回复。同一个消息再点一次＝停止。 */
  const speak = useCallback(async (raw: string, id?: string) => {
    if (!ttsEnabled) return;
    const key = id ?? "";
    if (speakingIdRef.current !== null && speakingIdRef.current === key) { stopSpeaking(); return; }
    stopSpeaking();
    const text = stripForSpeech(raw);
    if (!text) return;
    speakingIdRef.current = key;
    setSpeakingId(key);
    // ① 本地语音服务（装好之前这里会失败 → 直接落 ②，用户无感）
    try {
      const resp = await authFetch("/v1/synthesize_speech", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text,
          // 本地音色优先；系统音色名服务不认，发过去只会 500（见 ttsLocalVoices 的说明）
          voice: ttsLocalVoices.includes(ttsLocalVoice) ? ttsLocalVoice : undefined,
          speed: ttsRate,
          // 音高永远发：滑杆是唯一真相源。之前"等于 1 就不发"会让 config 里的
          // 旧值（1.15）偷偷生效 —— 滑杆拉回 1.00 却还是变调的，所见非所得。
          pitch: ttsPitch,
          emotion: ttsEmotion || undefined,
        }),
        signal: AbortSignal.timeout(ttsTimeoutMs),
      });
      const ctype = resp.headers.get("content-type") || "";
      if (resp.ok && !ctype.includes("application/json")) {
        const url = URL.createObjectURL(await resp.blob());
        const audio = new Audio(url);
        ttsAudioRef.current = audio;
        audio.onended = () => {
          URL.revokeObjectURL(url);
          if (speakingIdRef.current === key) { speakingIdRef.current = null; void Promise.resolve().then(() => setSpeakingId(null)); }
        };
        await audio.play();
        return;
      }
    } catch { /* 服务不可用 → 系统语音 */ }
    // ② 系统语音兜底
    if (!speechSupported()) {
      showToast(t("toast.tts_unsupported"));
      speakingIdRef.current = null;
      setSpeakingId(null);
      return;
    }
    systemSpeak(text);
  }, [ttsEnabled, ttsRate, ttsTimeoutMs, ttsLocalVoices, ttsLocalVoice,
      ttsPitch, ttsEmotion, stopSpeaking, systemSpeak, showToast, t]);

  // 切会话/新会话时停掉上一轮的朗读，别让它在后台继续念
  useEffect(() => { void Promise.resolve().then(() => stopSpeaking()); }, [session.id, stopSpeaking]);

  /* ── API Test ── */

  return { speakingId, stopSpeaking, speak };
}
