import { describe, expect, it } from "vitest";

/** 这一组测试是给"死键清理"兜底的。
 *
 * 背景：0.3.39/0.3.40 里我按"全代码库没有字面引用"删了 23 个 i18n 键，其中
 * `sidebar.filter_today/week/month/older` **其实是动态拼出来的**
 * （`t("sidebar.filter_" + f)`）→ 界面上直接显示成了裸键名。
 * 那次错误能发出门，是因为只有"人能看出来"这一道关。这里把它变成测试。
 */

// 用 Vite 的 glob 读源码：不依赖 node 类型，vitest 与 tsc 都认
const sources = Object.entries(
  import.meta.glob("../**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true }) as Record<string, string>,
)
  .filter(([f]) => !f.endsWith(".test.ts") && !f.includes("translations.ts"))
  .map(([, text]) => text);
const blob = sources.join("\n");
const table = Object.values(
  import.meta.glob("./translations.ts", { query: "?raw", import: "default", eager: true }) as Record<string, string>,
)[0];

const defined = new Set(
  [...table.matchAll(/^\s*"([a-z0-9_.]+)":\s*\{\s*zh:/gm)].map((m) => m[1]),
);

describe("i18n 键完整性", () => {
  it("每个 t(\"字面键\") 都在翻译表里", () => {
    const missing = new Set<string>();
    for (const src of sources) {
      for (const m of src.matchAll(/\bt\("([a-z0-9_.]+)"/g)) {
        const k = m[1];
        // 以 . 或 _ 结尾的是拼接前缀（t("sidebar.filter_" + f)），不是键
        if (k.endsWith(".") || k.endsWith("_")) continue;
        if (!defined.has(k)) missing.add(k);
      }
    }
    expect([...missing]).toEqual([]);
  });

  it("被拼接的键前缀，翻译表里至少还有一个键（防整族被删）", () => {
    // 匹配 "prefix." + x 与 "prefix_" + x 两种拼法
    const prefixes = new Set<string>();
    for (const src of sources) {
      for (const m of src.matchAll(/["'`]([a-z0-9_.]+?[._])["'`]?\s*\+/g)) prefixes.add(m[1]);
    }
    const broken = [...prefixes].filter(
      (p) => ![...defined].some((k) => k.startsWith(p)),
    );
    expect(broken).toEqual([]);
  });

  it("翻译表里没有重复键（重复会让 tsc 报 TS1117，且后者覆盖前者）", () => {
    const keys = [...table.matchAll(/^\s*"([a-z0-9_.]+)":\s*\{\s*zh:/gm)].map((m) => m[1]);
    const dup = keys.filter((k, i) => keys.indexOf(k) !== i);
    expect([...new Set(dup)]).toEqual([]);
  });

  it("动态枚举的键（全代码库搜得到名字的）都必须存在", () => {
    // 反向保险：键名作为**数据**出现在代码里（如 name: "session.default"）时，
    // 也要求它在表里 —— 这类键走 t(变量)，字面引用检查看不到。
    const missing: string[] = [];
    for (const m of blob.matchAll(/["']((?:sidebar|session|nav|agent|ctx|local|tools|chat|page|settings|skills|sse|channels)\.[a-z0-9_.]+)["']/g)) {
      const k = m[1];
      // 以分隔符结尾的是"拼接前缀"（如 "sidebar.filter_" + f），不是键本身，跳过
      if (k.endsWith(".") || k.endsWith("_")) continue;
      if (!defined.has(k) && !k.includes("$")) missing.push(k);
    }
    expect([...new Set(missing)]).toEqual([]);
  });
});
