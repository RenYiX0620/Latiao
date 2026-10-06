import { forwardRef, useImperativeHandle, useRef, useState } from "react";

/** 暴露给 App/ChatView 的输入框操作句柄（发送取值、语音注入、清空、聚焦）。 */
export interface ChatInputApi {
  getText(): string;
  setText(t: string): void;
  appendText(t: string): void;
  focus(): void;
}

/**
 * 聊天输入框（2026-10-06，性能重构）。
 *
 * 之前 textarea 是受控组件、value 住在 App 顶层：每敲一键 setPrompt 触发
 * 整个应用重渲染（含聊天区全部消息的 Markdown 树）——消息越多打字越卡，
 * 拼音组合期间每个候选更新都排队重渲染，字"一个一个蹦"（用户实测）。
 *
 * 现在：**非受控 textarea**（输入法直接写 DOM，组合期间 React 零干预）+
 * textRef 保存当前值；`hasText` 只驱动 is-empty 样式、150ms 节流——重渲染
 * 范围仅本组件。取值/注入/清空全部走 ref API，不再经过任何顶层 state。
 * 组合缓冲（compositionend → Enter 判定）由父组件回调保持不变。
 */
const ChatInput = forwardRef<ChatInputApi, {
  onKeyDown: (e: React.KeyboardEvent) => void;
  onPaste: (e: React.ClipboardEvent) => void;
  onCompositionStart: () => void;
  onCompositionEnd: () => void;
  placeholder: string;
}>(function ChatInput({ onKeyDown, onPaste, onCompositionStart, onCompositionEnd, placeholder }, ref) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const textRef = useRef("");
  const [hasText, setHasText] = useState(false);
  const lastSync = useRef(0);

  const syncHasText = (force = false) => {
    const now = Date.now();
    // 节流 120ms：hasText 只影响占位样式，不值得每键重渲染；
    // 组合结束（字上屏）时强制同步一次，保证按钮态及时
    if (force || now - lastSync.current > 120) {
      lastSync.current = now;
      setHasText(!!textRef.current.trim());
    }
  };

  useImperativeHandle(ref, () => ({
    getText: () => textRef.current,
    setText: (t: string) => {
      textRef.current = t;
      if (taRef.current) taRef.current.value = t;
      setHasText(!!t.trim());
    },
    appendText: (t: string) => {
      const next = textRef.current ? `${textRef.current} ${t}` : t;
      textRef.current = next;
      if (taRef.current) {
        taRef.current.value = next;
        taRef.current.focus();
      }
      setHasText(!!next.trim());
    },
    focus: () => taRef.current?.focus(),
  }), []);

  return (
    <textarea
      ref={taRef}
      className={`chat-input${hasText ? "" : " is-empty"}`}
      defaultValue=""
      onInput={(e) => {
        textRef.current = (e.target as HTMLTextAreaElement).value;
        syncHasText();
      }}
      onCompositionStart={onCompositionStart}
      onCompositionEnd={() => {
        textRef.current = taRef.current?.value ?? textRef.current;
        syncHasText(true);
        onCompositionEnd();
      }}
      onKeyDown={(e) => {
        // 组合结束后 keydown 先于/伴随最终上屏：读 DOM 实时值，别用滞后的 ref
        textRef.current = (e.target as HTMLTextAreaElement).value;
        onKeyDown(e);
      }}
      onPaste={onPaste}
      placeholder={placeholder}
      rows={1}
      style={{ resize: "none", minHeight: 52, maxHeight: 150 }}
    />
  );
});

export default ChatInput;
