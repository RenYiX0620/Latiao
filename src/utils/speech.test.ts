import { describe, expect, it } from "vitest";

import { localVoicesForLang, pickVoice, speechSupported, splitSentences, stripForSpeech, voicesForLang } from "./speech";

describe("stripForSpeech", () => {
  it("整块丢掉围栏代码（含语言标记）", () => {
    // 替换成换行（不是空格）后收尾折叠空行 → 语音引擎读起来就是两句
    const out = stripForSpeech("先看这段：\n```python\nprint('hi')\n```\n就这些。");
    expect(out).toBe("先看这段：\n就这些。");
  });

  it("未闭合的围栏（流式半截）也丢掉", () => {
    expect(stripForSpeech("结果如下：\n```json\n{\"a\": 1")).toBe("结果如下：");
  });

  it("行内代码保留内容、只去反引号", () => {
    expect(stripForSpeech("用 `read_file` 读一下")).toBe("用 read_file 读一下");
  });

  it("表格整行丢弃", () => {
    const md = "对比：\n| 指数 | 涨跌 |\n| --- | --- |\n| 上证 | +0.97% |\n完了";
    expect(stripForSpeech(md)).toBe("对比：\n完了");
  });

  it("链接留文字、图片留 alt、裸 URL 去掉", () => {
    expect(stripForSpeech("见[文档](https://x.com/a)和![图](https://x.com/b.png)"))
      .toBe("见文档和图");
    expect(stripForSpeech("地址 https://example.com/x 别念")).toBe("地址 别念");
  });

  it("剥标题/引用/列表标记与分隔线", () => {
    const out = stripForSpeech("## 标题\n> 引用\n- 一\n1. 二\n---\n正文");
    expect(out).toBe("标题\n引用\n一\n二\n正文");
  });

  it("剥强调符号但保留文字", () => {
    expect(stripForSpeech("**重要**的 _事_ 和 ~~旧的~~")).toBe("重要的 事 和 旧的");
  });

  it("去掉 emoji（引擎会念成“表情符号”）", () => {
    expect(stripForSpeech("好的 😊 稍等")).toBe("好的 稍等");
  });

  it("纯代码块 → 空串（不朗读）", () => {
    expect(stripForSpeech("```\ncode only\n```")).toBe("");
  });

  it("普通中文正文原样保留", () => {
    expect(stripForSpeech("今天大盘普涨，上证 +0.97%。")).toBe("今天大盘普涨，上证 +0.97%。");
  });
});

describe("splitSentences", () => {
  it("按句号切分并保留标点", () => {
    expect(splitSentences("第一句。第二句！第三句？"))
      .toEqual(["第一句。第二句！第三句？"]);
  });

  it("过短的碎片会合并（减少引擎启停）", () => {
    expect(splitSentences("好。行。可以。")).toEqual(["好。行。可以。"]);
  });

  it("超长句按逗号二次切分", () => {
    const long = "这一句话特别长，" + "内容".repeat(80) + "，结束。";
    const out = splitSentences(long, 40);
    expect(out.length).toBeGreaterThan(1);
    for (const s of out) expect(s.length).toBeLessThanOrEqual(41);
    expect(out.join("")).toContain("结束。");
  });

  it("空输入返回空数组", () => {
    expect(splitSentences("")).toEqual([]);
    expect(splitSentences("   \n  ")).toEqual([]);
  });

  it("换行与多空格归一", () => {
    expect(splitSentences("第一行\n\n第二行")).toEqual(["第一行 第二行"]);
  });
});

describe("pickVoice", () => {
  const v = (lang: string, name: string) => ({ lang, name, default: false } as SpeechSynthesisVoice);

  it("优先完全匹配的语言", () => {
    const voices = [v("en-US", "Samantha"), v("zh-CN", "Tingting")];
    expect(pickVoice(voices, "zh-CN")?.name).toBe("Tingting");
  });

  it("退一步按语言前缀匹配", () => {
    const voices = [v("zh-TW", "Meijia"), v("en-US", "Samantha")];
    expect(pickVoice(voices, "zh-CN")?.name).toBe("Meijia");
  });

  it("下划线写法也能匹配（zh_CN）", () => {
    const voices = [v("zh_CN", "Tingting")];
    expect(pickVoice(voices, "zh-CN")?.name).toBe("Tingting");
  });

  it("没有匹配音色时返回 undefined（交给系统默认）", () => {
    expect(pickVoice([v("en-US", "Samantha")], "zh-CN")).toBeUndefined();
    expect(pickVoice(undefined, "zh-CN")).toBeUndefined();
  });
});

describe("speechSupported", () => {
  it("在 jsdom 里没有 speechSynthesis 时返回 false（不崩）", () => {
    expect(typeof speechSupported()).toBe("boolean");
  });
});

describe("voicesForLang 只列当前语言的系统语音", () => {
  const all = [
    { name: "Tingting", lang: "zh-CN" },
    { name: "Shelley", lang: "zh-CN" },
    { name: "Samantha", lang: "en-US" },
    { name: "Daniel", lang: "en-GB" },
    { name: "Kyoko", lang: "ja-JP" },
    { name: "Milena", lang: "ru-RU" },
    { name: "Odd", lang: "" },
  ];

  it("中文只留中文语音（macOS 的 185 个里中文只有 10 个）", () => {
    const got = voicesForLang(all, "zh").map((v: { name: string }) => v.name);
    expect(got).toEqual(["Tingting", "Shelley"]);
  });

  it("换语言就换成那个语言的语音", () => {
    expect(voicesForLang(all, "en").map((v: { name: string }) => v.name)).toEqual(["Samantha", "Daniel"]);
    expect(voicesForLang(all, "ja").map((v: { name: string }) => v.name)).toEqual(["Kyoko"]);
    expect(voicesForLang(all, "ru").map((v: { name: string }) => v.name)).toEqual(["Milena"]);
  });

  it("zh_CN 这种下划线写法也能匹配", () => {
    expect(voicesForLang([{ name: "A", lang: "zh_CN" }], "zh").map((v: { name: string }) => v.name)).toEqual(["A"]);
  });

  it("一个都匹配不上时回落全量（下拉不能是空的）", () => {
    const only = [{ name: "Samantha", lang: "en-US" }];
    expect(voicesForLang(only, "zh")).toEqual(only);
  });

  it("空输入不炸", () => {
    expect(voicesForLang(undefined, "zh")).toEqual([]);
    expect(voicesForLang([], "zh")).toEqual([]);
  });
});

describe("localVoicesForLang 本地音色按界面语言筛", () => {
  const voices = ["女声001", "男声100", "克隆·婷婷", "英文女·heart", "西语女·dora", "怪名字"];
  const langs = { "女声001": "zh", "男声100": "zh", "克隆·婷婷": "zh",
                  "英文女·heart": "en", "西语女·dora": "es", "怪名字": "" };

  it("中文界面只留中文音色，加语言未知的", () => {
    expect(localVoicesForLang(voices, langs, "zh")).toEqual(["女声001", "男声100", "克隆·婷婷", "怪名字"]);
  });

  it("英文界面只留英文音色 + 语言未判定的（英文音色念中文会念歪，所以要筛）", () => {
    expect(localVoicesForLang(voices, langs, "en")).toEqual(["英文女·heart", "怪名字"]);
  });

  it("该语言一个音色都没有时回落全表（俄语界面不该只剩一个怪名字）", () => {
    expect(localVoicesForLang(voices, langs, "ru")).toEqual(voices);
  });

  it("空输入不炸", () => {
    expect(localVoicesForLang([], {}, "zh")).toEqual([]);
  });
});

describe("localVoicesForLang 的 keep：已选音色必须始终在列表里", () => {
  const voices = ["女声001", "英文女·heart"];
  const langs = { "女声001": "zh", "英文女·heart": "en" };

  it("切到中文后，之前选的英文音色仍留在列表（否则显示与实际不一致）", () => {
    expect(localVoicesForLang(voices, langs, "zh", "英文女·heart"))
      .toEqual(["英文女·heart", "女声001"]);
  });

  it("已选项本来就在列表里时不重复添加", () => {
    expect(localVoicesForLang(voices, langs, "zh", "女声001")).toEqual(["女声001"]);
  });

  it("keep 传了不存在的音色时忽略", () => {
    expect(localVoicesForLang(voices, langs, "zh", "不存在")).toEqual(["女声001"]);
  });
});
