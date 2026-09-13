"""本地模型基准测试：一键测出"能跑多快 + 能不能跑得动"，并给出可用建议。

测什么（都是对当前已加载引擎的真实请求，不掺估算）：
- 生成速度 tok/s 与首字延迟 TTFT：流式生成固定 token 预算；
- 预填充速度 prefill tok/s：投喂固定长度输入、max_tokens=1，量"读完输入"的耗时
  （长文档场景的瓶颈就在这一步——09-12 的 13.7 万字符输入即此）；
- 峰值内存 RSS：读引擎进程常驻内存（含 KV cache）。

产出：
- 记录追加到 ~/.local-ai-os/benchmarks.json（跨次/跨模型/跨上下文可比）；
- 规则化建议（不做玄学评分，只给可执行结论与阈值提示）。
"""
from __future__ import annotations

import json
import logging
import platform
import subprocess
import time
from pathlib import Path

import httpx

logger = logging.getLogger("latiao-sidecar")

BENCH_FILE = Path.home() / ".local-ai-os" / "benchmarks.json"
MAX_KEEP = 60                     # 保留最近 60 次记录
GEN_TOKENS = 256                  # 生成测试预算
PREFILL_SMALL_CHARS = 6_000       # ≈4K token（中文约 1.5 字符/token）
PREFILL_LARGE_CHARS = 48_000      # ≈32K token

_FILLER = ("本项目包含设计、采购、施工与调试阶段的完整工作范围，投标人须逐条响应并按序编号。"
           "技术要求以第七章为准，商务条款以第四章为准，任何偏离均须在偏离表中单独说明。\n")


def _engine_pid(port: int) -> int | None:
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=5)
        pid = (out.stdout or "").strip().split("\n")[0]
        return int(pid) if pid else None
    except Exception:
        return None


def engine_rss_gb(port: int) -> float | None:
    """引擎进程常驻内存（GB）。含模型权重 + KV cache + 计算缓冲。"""
    pid = _engine_pid(port)
    if not pid:
        return None
    try:
        import psutil  # bundled python 自带
        return round(psutil.Process(pid).memory_info().rss / 1024 ** 3, 2)
    except Exception:
        try:
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5)
            kb = float((out.stdout or "0").strip() or 0)
            return round(kb / 1024 / 1024, 2) if kb else None
        except Exception:
            return None


def total_ram_gb() -> float | None:
    try:
        import psutil
        return round(psutil.virtual_memory().total / 1024 ** 3, 1)
    except Exception:
        return None


async def _stream_generate(client: httpx.AsyncClient, url: str, headers: dict,
                           model: str, prompt: str, max_tokens: int) -> dict:
    """流式生成一次：返回 {ttft_s, total_s, tokens, usage}。token 数优先取 usage，
    否则以 SSE delta 计数近似（llama.cpp/mlx 每 delta ≈ 1 token）。"""
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "stream": True, "max_tokens": max_tokens, "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False}}
    t0 = time.monotonic()
    ttft = None
    deltas = 0
    usage = None
    async with client.stream("POST", url, json=body, headers=headers) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            try:
                d = json.loads(payload)
            except Exception:
                continue
            if d.get("usage"):
                usage = d["usage"]
            choices = d.get("choices") or [{}]
            delta = choices[0].get("delta") or {}
            if delta.get("content") or delta.get("reasoning_content"):
                if ttft is None:
                    ttft = time.monotonic() - t0
                deltas += 1
    total = time.monotonic() - t0
    tokens = int((usage or {}).get("completion_tokens") or deltas or 0)
    return {"ttft_s": round(ttft or total, 2), "total_s": round(total, 2),
            "tokens": tokens, "usage": usage}


async def _prefill(client: httpx.AsyncClient, url: str, headers: dict, model: str,
                   chars: int) -> dict:
    """预填充测试：投喂 chars 个字符、只生成 1 token，量"读完输入"的耗时。"""
    filler = (_FILLER * (chars // len(_FILLER) + 1))[:chars]
    prompt = ("以下是一份长文档的片段。请只回答一个字：好。\n\n" + filler)
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "stream": False, "max_tokens": 1, "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False}}
    t0 = time.monotonic()
    r = await client.post(url, json=body, headers=headers)
    r.raise_for_status()
    dt = time.monotonic() - t0
    return {"chars": chars, "seconds": round(dt, 2)}


async def run_benchmark(api_url: str, headers: dict, model: str,
                        context_limit: int, port: int = 1235,
                        large: bool = True) -> dict:
    """跑一轮基准测试（生成 + 预填充 + 内存），返回结果与建议。"""
    import local_llm
    name = getattr(local_llm._engine, "current_model_name", "") or model
    try:
        backend = str((local_llm.get_status() or {}).get("backend") or "")
    except Exception:
        backend = getattr(local_llm._engine, "backend", "") or ""
    res: dict = {"ts": int(time.time()), "model": name, "backend": backend,
                 "context_limit": context_limit, "platform": platform.platform()}
    timeout = httpx.Timeout(connect=10, read=600, write=60, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # 1) 生成速度 + 首字延迟
        g = await _stream_generate(client, api_url, headers, model,
                                   "请用三句话介绍你自己。", GEN_TOKENS)
        res.update({"gen_tokens": g["tokens"], "gen_seconds": g["total_s"],
                    "gen_tps": round(g["tokens"] / g["total_s"], 1) if g["total_s"] else 0,
                    "ttft_s": g["ttft_s"]})
        # 2) 预填充（小 + 大，大块按上下文余量决定是否跑）
        pf = []
        pf.append(await _prefill(client, api_url, headers, model, PREFILL_SMALL_CHARS))
        if large and context_limit >= 32768:
            try:
                pf.append(await _prefill(client, api_url, headers, model, PREFILL_LARGE_CHARS))
            except Exception as e:
                logger.warning("大块 prefill 测试失败：%s", e)
        for item in pf:
            item["tps"] = round(item["chars"] / 1.5 / max(item["seconds"], 0.01), 1)
        res["prefill"] = pf
    res["rss_gb"] = engine_rss_gb(port)
    res["total_ram_gb"] = total_ram_gb()
    res["advice"] = _advice(res)
    _append(res)
    return res


def _advice(r: dict) -> list[str]:
    """规则化建议：只给可执行结论，不做玄学评分。"""
    out: list[str] = []
    tps = r.get("gen_tps") or 0
    ttft = r.get("ttft_s") or 0
    if tps >= 25:
        out.append(f"生成 {tps} tok/s：响应很快，适合日常问答与子代理任务。")
    elif tps >= 8:
        out.append(f"生成 {tps} tok/s：可接受，适合常规分析；长回答需等待。")
    else:
        out.append(f"生成仅 {tps} tok/s：偏慢——日常问答建议换更小模型或云端，"
                   "本地留给需要质量的深度任务。")
    if ttft >= 5:
        out.append(f"首字延迟 {ttft}s 偏高：通常是长上下文或输入过长导致，注意提示体积。")
    for p in r.get("prefill") or []:
        ktok = int(p["chars"] / 1.5 / 1000)
        if p["seconds"] >= 60:
            out.append(f"读入约 {ktok}K token 需 {p['seconds']}s：长文档任务建议先经"
                       "「长文档筛选」压缩输入，或改用云端模型。")
        elif p["seconds"] >= 15:
            out.append(f"读入约 {ktok}K token 需 {p['seconds']}s：中等耗时，可接受。")
    rss, ram = r.get("rss_gb"), r.get("total_ram_gb")
    if rss and ram:
        head = ram - rss
        if head < 4:
            out.append(f"内存余量仅 {head:.1f}GB（已用 {rss}GB / 共 {ram}GB）："
                       "再调大上下文或加载更大模型有爆内存风险。")
        else:
            out.append(f"内存余量 {head:.1f}GB（已用 {rss}GB / 共 {ram}GB）：尚有余地。")
    if not out:
        out.append("未采集到有效指标，请确认模型已加载后重试。")
    return out


def _append(res: dict) -> None:
    try:
        data = []
        if BENCH_FILE.exists():
            data = json.loads(BENCH_FILE.read_text(encoding="utf-8")) or []
        data.append(res)
        BENCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        BENCH_FILE.write_text(json.dumps(data[-MAX_KEEP:], ensure_ascii=False, indent=1),
                              encoding="utf-8")
    except Exception:
        logger.warning("基准结果写入失败", exc_info=True)


def history(limit: int = 20) -> list[dict]:
    try:
        if BENCH_FILE.exists():
            data = json.loads(BENCH_FILE.read_text(encoding="utf-8")) or []
            return data[-limit:][::-1]
    except Exception:
        logger.warning("基准历史读取失败", exc_info=True)
    return []
