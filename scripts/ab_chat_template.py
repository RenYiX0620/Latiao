#!/usr/bin/env python3
"""模型模板 A/B 对照脚本（换模型 / 换 chat template 时复用）。

用途：同一个请求体分别打到两个引擎端口，各自跑一遍"带工具的对话循环"，
比较四件事——**工具调用轮次 / 原生 tool_calls / 文本式方言泄漏 / 回答质量**。
不碰正在用的引擎：A 端口用你现成的引擎，B 端口另起一个（换模板或换参数）。

用法：
  # 1) 另起 B 引擎（例：挂作者附带的 chat_template.jinja）
  E=/Applications/Latiao.app/Contents/Resources/sidecar/llama-upstream/llama-server
  $E -m /path/model.gguf --port 1236 --host 127.0.0.1 -c 128000 -ngl 999 \
     --cache-type-k q4_0 --cache-type-v q4_0 --parallel 2 \
     --chat-template-file /path/chat_template.jinja > /tmp/engine_B.log 2>&1 &
  # 等 /health 就绪（25B 级模型通常 10-60s，权重在页缓存里时几秒）

  # 2) 对照
  python3 scripts/ab_chat_template.py --a-port 1235 --b-port 1236 --runs 3

  # 3) 收尾（务必关掉 B 引擎，否则两份权重同时驻留）
  kill $(lsof -ti tcp:1236)

读法：两边轮次/原生工具数一样、都不泄漏方言 → 模板无差异，别改默认；
某一侧出现 markup_leak=True 或多花轮次 → 那一侧模板不适合这个模型。
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request

# 被测工具：故意用最小 schema，避免工具描述本身影响模型行为
TOOLS = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a file",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]}}},
    {"type": "function", "function": {"name": "list_dir", "description": "List a directory",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]}}},
]
SYS = "You are a helpful assistant with tools. Use them when needed."
PROMPT = ("请先用 read_file 分别读取 /tmp/abtest_a.txt 和 /tmp/abtest_b.txt（两次调用），"
          "然后用一句话说明两个文件各写了什么。")
# 文本式工具调用方言（不同模型/fine-tune 各有一套；native tool_calls 之外出现这些即"泄漏"）
MARKUP_RE = re.compile(r"<tool_call|<function=|\[TOOL_CALLS\]|```tool|<arg_key>", re.I)


def call(port: int, msgs: list) -> tuple[dict, float]:
    body = {"messages": msgs, "tools": TOOLS, "max_tokens": 1024,
            "temperature": 0.2, "stream": False}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    return json.load(urllib.request.urlopen(req, timeout=900)), time.monotonic() - t0


def run(port: int, tag: str, max_rounds: int = 5) -> dict:
    msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": PROMPT}]
    rounds, final = [], ""
    for i in range(max_rounds):
        try:
            resp, dt = call(port, msgs)
        except Exception as e:                       # 端口没起/超时：如实记录
            rounds.append({"round": i + 1, "error": str(e)[:80]})
            break
        m = resp["choices"][0]["message"]
        content = m.get("content") or ""
        tcs = m.get("tool_calls") or []
        think = m.get("reasoning_content") or m.get("reasoning") or ""
        u = resp.get("usage") or {}
        rounds.append({"round": i + 1, "sec": round(dt, 1), "native_tools": len(tcs),
                       "markup_leak": bool(MARKUP_RE.search(content)),
                       "content_chars": len(content), "think_chars": len(think),
                       "out_tok": u.get("completion_tokens")})
        if not tcs:
            final = content
            break
        msgs.append({"role": "assistant", "content": content or None, "tool_calls": tcs})
        for tc in tcs:                                # 真执行工具（读文件）
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except Exception:
                args = {}
            try:
                out = open(args.get("path", ""), encoding="utf-8").read()
            except Exception as e:
                out = f"错误：{e}"
            msgs.append({"role": "tool", "tool_call_id": tc.get("id") or "c", "content": out})
    return {"tag": tag, "rounds": rounds, "final": final}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a-port", type=int, default=1235, help="A 端口（现成引擎）")
    ap.add_argument("--b-port", type=int, default=1236, help="B 端口（换模板/换参数的引擎）")
    ap.add_argument("--runs", type=int, default=3, help="每个端口跑几轮（交错，抵消漂移）")
    args = ap.parse_args()

    # 测试文件（内容故意好认，便于判断回答质量）
    open("/tmp/abtest_a.txt", "w", encoding="utf-8").write("文件 A 的内容：辣条项目代号 Genesis。\n")
    open("/tmp/abtest_b.txt", "w", encoding="utf-8").write("文件 B 的内容：模型模板 A/B 测试中。\n")

    for n in range(1, args.runs + 1):
        for port, tag in ((args.a_port, "A"), (args.b_port, "B")):
            r = run(port, tag)
            rt = r["rounds"]
            print(f"  第{n}轮 {tag}(:{port}): 调用 {len(rt)} 次 "
                  f"原生工具={sum(x.get('native_tools', 0) for x in rt)} "
                  f"方言泄漏={any(x.get('markup_leak') for x in rt)} "
                  f"思考={[x.get('think_chars', 0) for x in rt]} "
                  f"耗时={[x.get('sec') for x in rt]}")
            print(f"        最终回答({len(r['final'])}字): {r['final'][:110]!r}")


if __name__ == "__main__":
    main()
