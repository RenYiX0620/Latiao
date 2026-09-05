"""50 轮自测驱动：真实云模型 + 桌面板块资金分析表（09-21 用户指令）。

用途：反复跑完整 agent 循环直到产出详尽分析；统计空名/恢复/守卫中止/完成度，
遇到问题即修复重跑（读仓库代码，不入生产路径）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sidecar"))

from api_routes import _extract_xlsx_text  # noqa: E402
from agent_loop import _resolve_api_target  # noqa: E402
from agent_loop_v2 import AgentLoop  # noqa: E402

XLSX = Path.home() / "Desktop" / "板块资金分析表.xlsx"
MODEL_NAME = "deepseek-v4-flash-vision-exp"
MAX_ROUNDS = 50
GOOD_MIN_CHARS = 600


def build_message() -> str:
    text = _extract_xlsx_text(XLSX.read_bytes())
    return (f"请分析我上传的「板块资金分析表.xlsx」。内容如下：\n\n{text[:12000]}")


async def one_round(round_no: int, msg: str, target) -> dict:
    protocol, api_url, headers, is_local = target
    assert protocol == "openai" and not is_local
    events = []
    empty_name_hits = 0
    guarded_abort = ""
    async for ev in AgentLoop(
        "cloud", [{"role": "user", "content": msg}], MODEL_NAME, api_url, headers,
        session_id=f"self-verify-{round_no}-{int(time.time()*1000)}",
        access_mode="full",
    ).run():
        events.append(ev)
        if isinstance(ev, dict) and (ev.get("content", "").startswith("⛔ 工具调用格式错误：工具名为空")
                                     or "工具名为空" in str(ev.get("content", ""))):
            empty_name_hits += 1
        if isinstance(ev, dict) and "模型连续 3 次输出空工具名" in str(ev.get("content", "")):
            guarded_abort = ev["content"]
    text = "".join(str(e.get("content", "")) for e in events if isinstance(e, dict) and e.get("content"))
    tool_events = sum(1 for e in events if isinstance(e, dict) and e.get("event") == "tool_start")
    return {
        "round": round_no,
        "tool_calls": tool_events,
        "empty_hits": empty_name_hits,
        "aborted": bool(guarded_abort),
        "final_len": len(text),
        "head": text[:80].replace("\n", " "),
        "events": events,
    }


async def main():
    cfg = json.loads((Path.home() / ".local-ai-os" / "config.json").read_text())
    entry = next(m for m in cfg["cloud_models"] if m["name"] == MODEL_NAME)
    target = await _resolve_api_target({"endpoint": entry["endpoint"], "key": entry.get("key", ""),
                                        "model": entry["name"], "protocol": entry.get("protocol", "openai")})
    msg = build_message()
    print(f"文件文本长度: {len(msg)} 字符 | 模型: {MODEL_NAME} | 目标: {target[1][:60]}")

    stats = Counter()
    for r in range(1, MAX_ROUNDS + 1):
        try:
            res = await one_round(r, msg, target)
        except Exception as e:  # noqa: BLE001
            print(f"[{r:02d}] 异常: {type(e).__name__}: {e}")
            stats["exception"] += 1
            continue
        stats["rounds"] += 1
        stats["tool_calls"] += res["tool_calls"]
        stats["empty_hits"] += res["empty_hits"]
        if res["aborted"]:
            stats["guarded_abort"] += 1
        print(f"[{r:02d}] tools={res['tool_calls']:2d} 空名={res['empty_hits']:2d} "
              f"中止={int(res['aborted'])} 文本={res['final_len']:4d} | {res['head'][:60]}")
        good = (res["final_len"] >= GOOD_MIN_CHARS and not res["aborted"]
                and "结论" in "".join(str(e.get("content")) for e in res["events"] if isinstance(e, dict)))
        stats["good" if good else "bad"] += 1
        if good and stats["good"] == 1:
            # 第一份成功分析存档
            Path("/tmp/self-verify-ok.txt").write_text(
                str(res["events"]), encoding="utf-8")
        if r % 10 == 0:
            print(f"  ... 进度 {r}/{MAX_ROUNDS} {dict(stats)}")

    total = stats["good"] + stats["bad"]
    print(f"\n=== 50 轮完成: 成功 {stats['good']} / 失败 {stats['bad']} | "
          f"工具调用 {stats['tool_calls']} 次 | 空名命中 {stats['empty_hits']} | "
          f"守卫中止 {stats['guarded_abort']} | 异常 {stats['exception']} ===")


if __name__ == "__main__":
    asyncio.run(main())
