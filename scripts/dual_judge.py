#!/usr/bin/env python3
"""双评审一致率评分器（LLM-as-judge + 评委自检协议）。

用途：给"一次只动一个变量"的 A/B 提供带自检的评委。同一批答案让两个
不同族的 OpenAI 兼容模型各判一遍，报告一致率——一致率低说明评分标准
含糊或评委有偏好，先修尺子再下任何结论（手法来源：EnSIMem 论文的
双评审模型一致性协议；评委输出解析用 raw_decode 扫描，同
sidecar/agent/parsing._extract_first_json_object）。

输入 JSONL 每行：
  {"id": "可选", "question": "问题", "answer": "待评回答", "reference": "可选参考答案"}

用法（例：云端 mimo + 本地引擎两个族）：
  python3 scripts/dual_judge.py items.jsonl \
    --judge1-base https://api.xiaomimimo.com/v1 --judge1-model mimo-v2.6-flash \
    --judge1-key-env MIMO_KEY \
    --judge2-base http://127.0.0.1:1235/v1 --judge2-model local \
    --out result.json

密钥从环境变量（--judgeN-key-env）或文件（--judgeN-key-file）读取，不进 argv。
评分规则默认内置"事实/记忆正确性"通用版，用 --rubric-file 覆盖。
本地引擎没在跑时先把模型加载起来（例如辣条当前模型）再执行。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 内置评分规则：事实/记忆正确性（通用版）。正式使用建议按被测维度改写。
DEFAULT_RUBRIC = """\
1. 提供了参考答案时：回答与参考在事实上一致 → pass=true；矛盾或遗漏核心事实 → pass=false。
2. 未提供参考答案时：回答对问题的直接信息需求给出正确且完整的核心事实 → pass=true。
3. 编造事实、答非所问、用含糊措辞回避问题 → pass=false。
4. 格式瑕疵（错别字、语序、标点）不作为扣分依据；score 反映确定程度（0=完全不可信，100=完全确定）。
"""


def extract_first_json_object(text: str) -> dict | None:
    """raw_decode 位置扫描取第一个可解析 JSON 对象（与 agent/parsing 同手法）。"""
    if not text:
        return None
    decoder = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, _end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            return obj
        i = text.find("{", i + 1)
    return None


def build_judge_messages(item: dict, rubric: str) -> list[dict]:
    """评委消息：规则+输出格式进 system，材料进 user（与被评内容隔离）。"""
    system = (
        "你是严格、不受文风偏好影响的评分评委。根据评分规则判断回答是否合格，"
        '只输出一个 JSON 对象：{"pass": true/false, "score": 0-100, "reason": "一句话理由"}，'
        "不要输出任何其他文字。\n评分规则：\n" + rubric.strip()
    )
    parts = [f"问题：{item.get('question', '').strip()}"]
    ref = (item.get("reference") or "").strip()
    if ref:
        parts.append(f"参考答案：{ref}")
    parts.append(f"待评回答：{(item.get('answer') or '').strip()}")
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(parts)}]


def call_judge(cfg: dict, messages: list[dict], timeout: float = 60) -> dict:
    """调一次 OpenAI 兼容 chat/completions；网络/5xx 重试一次；解析评委 JSON。"""
    base = cfg["base"].rstrip("/")
    body = json.dumps({"model": cfg["model"], "messages": messages,
                       "temperature": 0, "stream": False, "max_tokens": 300},
                      ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cfg.get("key"):
        headers["Authorization"] = f"Bearer {cfg['key']}"
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(base + "/chat/completions", data=body,
                                         headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise RuntimeError(f"{cfg['model']} HTTP {e.code}: {e.reason}") from e
            last_err = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
        if attempt == 0:
            time.sleep(2)
    else:
        raise RuntimeError(f"{cfg['model']} 调用失败: {last_err}")
    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
    obj = extract_first_json_object(content)
    if obj is None:
        raise RuntimeError(f"{cfg['model']} 未返回可解析 JSON: {content[:120]!r}")
    return obj


def judge_item(cfg: dict, item: dict, rubric: str,
               call_fn=call_judge) -> dict:
    """单个条目的评审结果（归一化：pass 布尔、score 0-100、reason 一句话）。"""
    out = call_fn(cfg, build_judge_messages(item, rubric),
                  timeout=float(cfg.get("timeout", 60)))
    try:
        score = max(0, min(100, int(out.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    return {"pass": bool(out.get("pass")), "score": score,
            "reason": str(out.get("reason", ""))[:200]}


def score(items: list[dict], judges: list[tuple[str, dict]], rubric: str,
          judge_fn=judge_item) -> dict:
    """跑全量并汇总。judge_fn 可注入（测试用）。"""
    rows = []
    for item in items:
        row: dict = {"id": item.get("id", ""), "question": item.get("question", "")}
        for tag, cfg in judges:
            try:
                row[tag] = judge_fn(cfg, item, rubric)
            except Exception as e:  # 评委挂了记为 None，不拖垮整批
                row[tag] = {"pass": None, "score": None, "reason": f"调用失败: {e}"}
        rows.append(row)
    t1, t2 = judges[0][0], judges[1][0]
    both = [r for r in rows
            if r[t1].get("pass") is not None and r[t2].get("pass") is not None]
    n_both = len(both)
    agree = sum(1 for r in both if r[t1]["pass"] == r[t2]["pass"])
    deltas = [abs(r[t1]["score"] - r[t2]["score"]) for r in both
              if r[t1].get("score") is not None and r[t2].get("score") is not None]
    summary = {
        "n_items": len(items),
        "n_scored_both": n_both,
        "pass_rate": {
            t1: round(sum(1 for r in both if r[t1]["pass"]) / n_both, 4) if n_both else None,
            t2: round(sum(1 for r in both if r[t2]["pass"]) / n_both, 4) if n_both else None,
        },
        "agreement_rate": round(agree / n_both, 4) if n_both else None,
        "mean_abs_score_delta": round(sum(deltas) / len(deltas), 2) if deltas else None,
    }
    disagreements = [r for r in both if r[t1]["pass"] != r[t2]["pass"]]
    return {"summary": summary, "items": rows, "disagreements": disagreements}


def _resolve_key(args_judge: argparse.Namespace, n: int) -> str | None:
    env_name = getattr(args_judge, f"judge{n}_key_env", None)
    if env_name:
        key = os.environ.get(env_name, "")
        if key:
            return key
    key_file = getattr(args_judge, f"judge{n}_key_file", None)
    if key_file:
        return Path(key_file).expanduser().read_text("utf-8").strip()
    return None


def load_items(path: str) -> list[dict]:
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not obj.get("question") or obj.get("answer") is None:
                raise SystemExit(f"第 {ln} 行缺少 question/answer: {line[:80]!r}")
            items.append(obj)
    return items


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="双评审一致率评分器")
    p.add_argument("items", help="输入 JSONL（每行 question/answer[/reference]）")
    for n in (1, 2):
        p.add_argument(f"--judge{n}-base", required=True, help=f"评委{n} 的 OpenAI 兼容 base")
        p.add_argument(f"--judge{n}-model", required=True, help=f"评委{n} 的模型名")
        p.add_argument(f"--judge{n}-key-env", default=None, help=f"评委{n} 密钥的环境变量名")
        p.add_argument(f"--judge{n}-key-file", default=None, help=f"评委{n} 密钥文件路径")
    p.add_argument("--rubric-file", default=None, help="评分规则文本（缺省用内置规则）")
    p.add_argument("--out", default=None, help="完整结果 JSON 输出路径")
    p.add_argument("--timeout", type=float, default=60)
    args = p.parse_args(argv)

    judges = []
    for n in (1, 2):
        judges.append((f"judge{n}", {
            "base": getattr(args, f"judge{n}_base"),
            "model": getattr(args, f"judge{n}_model"),
            "key": _resolve_key(args, n),
            "timeout": args.timeout,
        }))
    rubric = (Path(args.rubric_file).read_text("utf-8") if args.rubric_file
              else DEFAULT_RUBRIC)
    items = load_items(args.items)
    if len(items) < 2:
        raise SystemExit("至少需要 2 个条目（一致率才有意义）")

    result = score(items, judges, rubric)
    s = result["summary"]
    j1m, j2m = judges[0][1]["model"], judges[1][1]["model"]
    print("== 双评审一致率 ==")
    print(f"条目: {s['n_items']}（双侧都评出: {s['n_scored_both']}）")
    print(f"评委1({j1m}) 通过率: {s['pass_rate']['judge1']}")
    print(f"评委2({j2m}) 通过率: {s['pass_rate']['judge2']}")
    ar = s["agreement_rate"]
    print(f"一致率: {ar}")
    if ar is not None and ar < 0.85:
        print("⚠️ 一致率 <85%：评分标准可能含糊或评委偏好明显——先修尺子再下结论。")
    print(f"平均分差 |Δscore|: {s['mean_abs_score_delta']}")
    print(f"不一致条目: {len(result['disagreements'])} 个"
          + ("（详情见 out JSON）" if args.out else ""))
    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"完整结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
