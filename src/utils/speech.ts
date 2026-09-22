/**
 * 朗读用文本处理与系统语音工具（纯函数，便于单测）。
 *
 * 为什么要有这一层：**朗读前必须剥掉 Markdown 与代码块** —— 不剥的话
 * `**加粗**`、表格、```代码围栏```、裸链接都会被逐字念出来，是听感的分水岭。
 * 而语音这块此前**零测试**（当年 `_get_whisper_model` 就是因为没测试被误删过一次），
 * 所以这里只放纯函数、配单测，副作用（speechSynthesis / 音频播放）留在 App 里。
 */

const _SENTENCE_END = "。！？!?；;";

/** 剥掉 Markdown 与代码块，只留适合朗读的正文。 */
export function stripForSpeech(markdown: string): string {
  let t = markdown || "";

  // 围栏代码块整块丢弃（念代码没有意义），含未闭合的流式半截
  t = t.replace(/```[\s\S]*?```/g, "\n");
  t = t.replace(/~~~[\s\S]*?~~~/g, "\n");
  t = t.replace(/```[\s\S]*$/g, "\n");

  // 表格整行丢弃（逐格念毫无意义）；gfm 表格的数据行与分隔行都以 | 开头
  t = t.split("\n").filter((line) => !/^\s*\|/.test(line)).join("\n");

  // 行内代码保留内容（多为文件名/函数名，用户要听），只去反引号
  t = t.replace(/`([^`]*)`/g, "$1");

  // 图片 → alt 文本；链接 → 链接文字；裸 URL 去掉（念出来是噪音）
  t = t.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");
  t = t.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
  t = t.replace(/https?:\/\/\S+/g, " ");

  // HTML 标签
  t = t.replace(/<[^>]+>/g, " ");

  // 标题 / 引用 / 列表标记 / 分隔线
  t = t.replace(/^\s{0,3}#{1,6}\s*/gm, "");
  t = t.replace(/^\s{0,3}>\s?/gm, "");
  t = t.replace(/^\s{0,3}(?:[-*+]|\d{1,2}[.)])\s+/gm, "");
  t = t.replace(/^\s*([-*_])\1{2,}\s*$/gm, "\n");

  // 强调符号：只剥"成对包裹"的标记，**绝不能碰标识符里的下划线**
  // （`read_file` 曾被剥成 `readfile` —— 由单测抓到）
  t = t.replace(/\*\*([^*]+)\*\*/g, "$1");
  t = t.replace(/__([^_]+)__/g, "$1");
  t = t.replace(/\*([^*]+)\*/g, "$1");
  t = t.replace(/(^|[^A-Za-z0-9_])_([^_]+)_(?!\w)/g, "$1$2");
  t = t.replace(/~~([^~]+)~~/g, "$1");

  // emoji：多数引擎会念成"表情符号"或读出名字，朗读时去掉
  t = t.replace(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}\u{2190}-\u{21FF}]/gu, "");

  // 收尾：去掉只剩空白的行（代码块/分隔线被替换后留下的），再折叠空行
  return t
    .replace(/^[ \t]+$/gm, "")
    .replace(/[ \t]+/g, " ")
    .replace(/\n{2,}/g, "\n")
    .trim();
}

/**
 * 按句切分：一次塞几千字会让引擎卡住，逐句播也更像"在说话"。
 * 过短的碎片会合并，减少引擎启停次数。
 */
export function splitSentences(text: string, maxLen = 110): string[] {
  const src = (text || "").replace(/\s+/g, " ").trim();
  if (!src) return [];

  // 手写扫描而不是正则 lookbehind（旧 WebKit 不支持 lookbehind，且会让整个模块解析失败）
  const parts: string[] = [];
  let buf = "";
  for (const ch of src) {
    buf += ch;
    if (_SENTENCE_END.includes(ch)) {
      if (buf.trim()) parts.push(buf.trim());
      buf = "";
    }
  }
  if (buf.trim()) parts.push(buf.trim());

  const out: string[] = [];
  for (const part of parts) {
    if (part.length <= maxLen) {
      out.push(part);
      continue;
    }
    let rest = part;
    while (rest.length > maxLen) {
      const cut = Math.max(
        rest.lastIndexOf("，", maxLen),
        rest.lastIndexOf("、", maxLen),
        rest.lastIndexOf(",", maxLen),
        rest.lastIndexOf(" ", maxLen),
      );
      const at = cut >= Math.floor(maxLen / 2) ? cut : maxLen;
      out.push(rest.slice(0, at + 1).trim());
      rest = rest.slice(at + 1).trim();
    }
    if (rest) out.push(rest);
  }

  const merged: string[] = [];
  for (const s of out) {
    const last = merged[merged.length - 1];
    if (last && (last + s).length <= maxLen) merged[merged.length - 1] = last + s;
    else merged.push(s);
  }
  return merged;
}

/** 从系统音色里挑一个与界面语言匹配的；找不到返回 undefined（交给系统默认音色）。 */
export function pickVoice(
  voices: SpeechSynthesisVoice[] | undefined | null,
  lang: string,
): SpeechSynthesisVoice | undefined {
  const want = (lang || "zh").toLowerCase().replace("_", "-");
  const short = want.split("-")[0];
  const list = (voices || []).filter((v) => !!v);
  const norm = (v: SpeechSynthesisVoice) => (v.lang || "").toLowerCase().replace("_", "-");
  return (
    list.find((v) => norm(v) === want) ||
    list.find((v) => norm(v).startsWith(short + "-")) ||
    list.find((v) => norm(v) === short) ||
    undefined
  );
}

/** 这台设备有没有系统语音能力（没有就完全不显示朗读按钮，而不是点了报错）。 */
export function speechSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    !!window.speechSynthesis &&
    typeof window.SpeechSynthesisUtterance === "function"
  );
}
