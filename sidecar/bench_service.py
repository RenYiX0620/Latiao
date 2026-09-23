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
                        large: bool = True, lang: str = "zh") -> dict:
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
    res["advice"] = _advice(res, lang)
    _append(res)
    return res


# 建议文案：后端按调用方传来的界面语言出（以前写死中文，英文界面里会突兀地插一段中文）
_ADVICE_TEXT = {
    "fast": {
        "zh": "生成 {tps} tok/s：响应很快，适合日常问答与子代理任务。",
        "en": "Generation {tps} tok/s: very responsive — good for everyday Q&A and sub-agent work.",
        "ja": "生成 {tps} tok/s：非常に速く、日常の質疑応答やサブエージェント向き。",
        "ru": "Генерация {tps} tok/s: очень быстро — подходит для обычных вопросов и суб-агентов.",
    },
    "ok": {
        "zh": "生成 {tps} tok/s：可接受，适合常规分析；长回答需等待。",
        "en": "Generation {tps} tok/s: acceptable for routine analysis; long answers will take a while.",
        "ja": "生成 {tps} tok/s：許容範囲。通常の分析向きで、長い回答は待ち時間が発生。",
        "ru": "Генерация {tps} tok/s: приемлемо для обычного анализа; длинные ответы придётся подождать.",
    },
    "slow": {
        "zh": "生成仅 {tps} tok/s：偏慢——日常问答建议换更小模型或云端，本地留给需要质量的深度任务。",
        "en": "Generation only {tps} tok/s: slow — use a smaller or cloud model for everyday chat and keep the local one for tasks that need quality.",
        "ja": "生成は {tps} tok/s のみ：遅め。日常の会話は小型モデルかクラウドへ、ローカルは品質が要る作業に。",
        "ru": "Генерация всего {tps} tok/s: медленно — для повседневных вопросов возьмите меньшую или облачную модель, локальную оставьте для задач, где важно качество.",
    },
    "ttft": {
        "zh": "首字延迟 {ttft}s 偏高：通常是长上下文或输入过长导致，注意提示体积。",
        "en": "First-token latency {ttft}s is high: usually a long context or oversized input — watch the prompt size.",
        "ja": "初回トークン遅延 {ttft}s は高め：長いコンテキストか入力過多が原因。プロンプト量に注意。",
        "ru": "Задержка первого токена {ttft}s велика: обычно из-за длинного контекста или слишком большого ввода.",
    },
    "prefill_slow": {
        "zh": "读入约 {ktok}K token 需 {sec}s：长文档任务建议先经「长文档筛选」压缩输入，或改用云端模型。",
        "en": "Reading ~{ktok}K tokens takes {sec}s: for long documents, compress the input with the document filter first or use a cloud model.",
        "ja": "約 {ktok}K トークンの読み込みに {sec}s：長文は「長文フィルタ」で圧縮するか、クラウドモデルを。",
        "ru": "Чтение ~{ktok}K токенов занимает {sec}s: для длинных документов сожмите ввод фильтром документов или возьмите облачную модель.",
    },
    "prefill_mid": {
        "zh": "读入约 {ktok}K token 需 {sec}s：中等耗时，可接受。",
        "en": "Reading ~{ktok}K tokens takes {sec}s: moderate, acceptable.",
        "ja": "約 {ktok}K トークンの読み込みに {sec}s：中程度で許容範囲。",
        "ru": "Чтение ~{ktok}K токенов занимает {sec}s: умеренно, приемлемо.",
    },
    "ram_tight": {
        "zh": "内存余量仅 {head:.1f}GB（已用 {rss}GB / 共 {ram}GB）：再调大上下文或加载更大模型有爆内存风险。",
        "en": "Only {head:.1f}GB RAM headroom ({rss}GB used of {ram}GB): raising the context or loading a bigger model risks running out.",
        "ja": "メモリ余裕は {head:.1f}GB のみ（使用 {rss}GB / 全 {ram}GB）：コンテキスト拡大や大型モデルは危険。",
        "ru": "Запас ОЗУ всего {head:.1f}GB (использовано {rss}GB из {ram}GB): увеличение контекста или большая модель рискуют исчерпать память.",
    },
    "ram_ok": {
        "zh": "内存余量 {head:.1f}GB（已用 {rss}GB / 共 {ram}GB）：尚有余地。",
        "en": "RAM headroom {head:.1f}GB ({rss}GB used of {ram}GB): comfortable.",
        "ja": "メモリ余裕 {head:.1f}GB（使用 {rss}GB / 全 {ram}GB）：まだ余裕あり。",
        "ru": "Запас ОЗУ {head:.1f}GB (использовано {rss}GB из {ram}GB): с запасом.",
    },
    "none": {
        "zh": "未采集到有效指标，请确认模型已加载后重试。",
        "en": "No usable metrics collected — make sure a model is loaded and try again.",
        "ja": "有効な指標が取れませんでした。モデルを読み込んでから再試行してください。",
        "ru": "Метрики не собраны — убедитесь, что модель загружена, и повторите.",
    },
}


def _t(key: str, lang: str, **kw) -> str:
    table = _ADVICE_TEXT.get(key) or {}
    text = table.get((lang or "zh").lower()) or table.get("en") or table.get("zh", "")
    try:
        return text.format(**kw) if kw else text
    except (KeyError, ValueError):
        return text


def _advice(r: dict, lang: str = "zh") -> list[str]:
    """规则化建议：只给可执行结论，不做玄学评分。"""
    out: list[str] = []
    tps = r.get("gen_tps") or 0
    ttft = r.get("ttft_s") or 0
    if tps >= 25:
        out.append(_t("fast", lang, tps=tps))
    elif tps >= 8:
        out.append(_t("ok", lang, tps=tps))
    else:
        out.append(_t("slow", lang, tps=tps))
    if ttft >= 5:
        out.append(_t("ttft", lang, ttft=ttft))
    for p in r.get("prefill") or []:
        ktok = int(p["chars"] / 1.5 / 1000)
        if p["seconds"] >= 60:
            out.append(_t("prefill_slow", lang, ktok=ktok, sec=p["seconds"]))
        elif p["seconds"] >= 15:
            out.append(_t("prefill_mid", lang, ktok=ktok, sec=p["seconds"]))
    rss, ram = r.get("rss_gb"), r.get("total_ram_gb")
    if rss and ram:
        head = ram - rss
        if head < 4:
            out.append(_t("ram_tight", lang, head=head, rss=rss, ram=ram))
        else:
            out.append(_t("ram_ok", lang, head=head, rss=rss, ram=ram))
    if not out:
        out.append(_t("none", lang))
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
