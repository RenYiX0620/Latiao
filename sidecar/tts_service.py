"""本地语音合成（TTS）代理：把前端的朗读请求转给一个**独立的本地语音服务**。

第一轮（框架先行、零下载）Latiao 侧只做两件事：

1. **代理**：`config.json` 里配了 `tts.base_url` 且服务在监听 → 转发到它的
   OpenAI 兼容端点（`POST {base_url}/v1/audio/speech`），把音频字节原样回给前端。
2. **降级**：服务没装/没启动 → 返回**结构化错误**（`code` + `next_steps`），
   前端据此直接回退系统语音 —— 不给用户看一句超时，也不让界面空着
   （同「失败要说清下一步」的原则）。

**模型不在这里**：语音服务是独立进程（`~/Models/latiao-tts/…`），换模型只改
`config.json` 的 `tts.model_id`，Latiao 的代码与前端都不用动 —— 这正是"框架先行、
模型后放"的分界。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional, Tuple

import httpx

logger = logging.getLogger("latiao-sidecar")

DEFAULT_TTS = {
    "enabled": True,                        # 关掉＝完全不碰语音服务，一律用系统语音
    "base_url": "http://127.0.0.1:7799",    # 本地语音服务（OpenAI 兼容）
    "model_id": "",                         # 服务端模型名；空＝用服务默认
    "voice": "",                            # 音色；空＝用服务默认
    "speed": 1.0,
    "max_chars": 3000,                       # 单次合成上限，超长由前端按句切
    # 单次合成超时（秒）。**必须与前端 speak() 的 8s AbortSignal 对齐**：
    # 实测（假服务"在监听但不响应"）30s 会让用户白等到超时才听到系统语音 —— 朗读这种
    # 动作的容错窗口只有几秒，本地合成一句本来 <2s，8s 已经是很宽的余量。
    "timeout": 8.0,
}

_PROBE_TTL = 20.0            # 可达性缓存：朗读是高频动作，别每次都探
_MAX_AUDIO_BYTES = 12 * 1024 * 1024
_probe_cache: dict = {"at": 0.0, "base": "", "ok": False}


def read_tts_config(config_file: Path) -> dict:
    """读取 config.json 的 tts 段并与默认值合并（缺失/损坏一律回落默认）。"""
    conf = dict(DEFAULT_TTS)
    try:
        if config_file.exists():
            raw = json.loads(config_file.read_text(encoding="utf-8"))
            section = raw.get("tts") if isinstance(raw, dict) else None
            if isinstance(section, dict):
                for key, value in section.items():
                    if key in conf and value is not None:
                        conf[key] = value
    except Exception:
        logger.warning("读取 tts 配置失败，使用默认值", exc_info=True)
    conf["base_url"] = str(conf.get("base_url") or "").rstrip("/")
    try:
        conf["speed"] = float(conf.get("speed") or 1.0)
    except Exception:
        conf["speed"] = 1.0
    conf["speed"] = min(2.0, max(0.5, conf["speed"]))
    return conf


def _request_timeout(conf: dict, probe: bool = False) -> float:
    if probe:
        return 1.5
    try:
        return max(3.0, float(conf.get("timeout") or DEFAULT_TTS["timeout"]))
    except Exception:
        return DEFAULT_TTS["timeout"]


def probe_service(conf: dict) -> bool:
    """语音服务是否在监听（短超时、结果带 TTL 缓存）。"""
    base = str(conf.get("base_url") or "").strip()
    if not base:
        return False
    now = time.monotonic()
    if _probe_cache["base"] == base and now - _probe_cache["at"] < _PROBE_TTL:
        return bool(_probe_cache["ok"])
    ok = False
    try:
        with httpx.Client(timeout=_request_timeout(conf, probe=True)) as client:
            client.get(f"{base}/v1/audio/voices")
        ok = True          # 有 HTTP 响应即视为在监听；内容对不对交给合成调用判断
    except Exception:
        ok = False
    _probe_cache.update({"at": now, "base": base, "ok": ok})
    if ok:
        logger.info("语音服务可用: %s", base)
    return ok


def _error(code: str, message: str, next_steps: list) -> dict:
    """结构化错误：前端只认 code，用户看到 message + next_steps（不是超时）。"""
    return {"status": "error", "code": code, "message": message, "next_steps": next_steps}


def unavailable_payload(conf: dict) -> dict:
    """服务未就绪时的标准回答。前端收到 code=tts_unavailable 就回退系统语音。"""
    return _error(
        "tts_unavailable",
        f"本地语音服务未运行（{conf.get('base_url') or '未配置'}）",
        [
            "已自动改用系统语音，朗读功能仍可用。",
            f"想要高质量音色：启动语音服务并确认它在监听 {conf.get('base_url') or '<地址>'}。",
            "或在设置里关闭「朗读回复」，不再尝试本地语音。",
        ],
    )


async def synthesize(text: str, voice: Optional[str], speed: Optional[float],
                     config_file: Path) -> Tuple[Optional[bytes], Optional[str], Optional[dict]]:
    """把文本转成音频字节。返回 (audio, content_type, error)，三选二。"""
    conf = read_tts_config(config_file)
    clean = (text or "").strip()
    if not clean:
        return None, None, _error("tts_empty_text", "没有需要朗读的文本", [])
    if not conf.get("enabled", True):
        return None, None, _error("tts_disabled", "「朗读回复」已在设置里关闭",
                                  ["在 设置 → 通用 里打开「朗读回复」。"])
    if not probe_service(conf):
        return None, None, unavailable_payload(conf)

    limit = int(conf.get("max_chars") or DEFAULT_TTS["max_chars"])
    if len(clean) > limit:
        clean = clean[:limit]

    payload = {
        "model": str(conf.get("model_id") or "kokoro"),
        "input": clean,
        "voice": str(voice or conf.get("voice") or ""),
        "speed": float(speed or conf.get("speed") or 1.0),
        "response_format": "wav",
    }
    try:
        async with httpx.AsyncClient(timeout=_request_timeout(conf)) as client:
            resp = await client.post(f"{conf['base_url']}/v1/audio/speech", json=payload)
    except Exception as e:
        logger.warning("语音服务调用失败: %s", e)
        _probe_cache.update({"at": 0.0, "base": conf.get("base_url", ""), "ok": False})
        return None, None, unavailable_payload(conf)

    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
    if resp.status_code >= 400 or ctype.startswith("application/json"):
        detail = ""
        try:
            body = resp.json()
            detail = str(body.get("detail") or body.get("message") or body.get("error") or "")
        except Exception:
            detail = resp.text[:200]
        logger.warning("语音服务返回 %s: %s", resp.status_code, detail)
        return None, None, _error(
            "tts_service_error",
            f"语音服务返回 {resp.status_code}{('：' + detail) if detail else ''}",
            ["已自动改用系统语音。",
             "若反复出现：检查语音服务的模型是否加载成功（服务日志）。"],
        )
    audio = resp.content or b""
    if not audio:
        return None, None, _error("tts_empty_audio", "语音服务返回了空音频", [])
    if len(audio) > _MAX_AUDIO_BYTES:
        return None, None, _error("tts_audio_too_large",
                                  f"音频过大（{len(audio) // 1048576}MB），请分段朗读", [])
    return audio, (ctype or "audio/wav"), None


async def list_voices(config_file: Path) -> dict:
    """代理语音服务的音色列表。前端音色下拉**按当前模型动态取** —— 换模型不用改前端。"""
    conf = read_tts_config(config_file)
    if not conf.get("enabled", True):
        return _error("tts_disabled", "「朗读回复」已在设置里关闭", [])
    if not probe_service(conf):
        return _error("tts_unavailable", unavailable_payload(conf)["message"],
                      unavailable_payload(conf)["next_steps"])
    try:
        async with httpx.AsyncClient(timeout=_request_timeout(conf, probe=True)) as client:
            resp = await client.get(f"{conf['base_url']}/v1/audio/voices")
        if resp.status_code >= 400:
            return _error("tts_service_error", f"语音服务返回 {resp.status_code}", [])
        data = resp.json()
    except Exception as e:
        logger.warning("读取音色列表失败: %s", e)
        return _error("tts_unavailable", f"读取音色列表失败：{e}", [])
    voices = data.get("voices") if isinstance(data, dict) else data
    if not isinstance(voices, list):
        voices = []
    return {"status": "success", "voices": voices, "default": conf.get("voice") or ""}


def status(config_file: Path) -> dict:
    """给设置页看的一行状态：是否启用、服务是否在跑、当前模型与音色。"""
    conf = read_tts_config(config_file)
    enabled = bool(conf.get("enabled", True))
    return {
        "enabled": enabled,
        "available": probe_service(conf) if enabled else False,
        "base_url": conf.get("base_url") or "",
        "model_id": conf.get("model_id") or "",
        "voice": conf.get("voice") or "",
        "speed": conf.get("speed"),
    }
