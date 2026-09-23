"""模型路由（agent_loop.py 拆出的第七块，2026-09-23）。

_resolve_api_target / _get_best_cloud_config：决定这一轮打到本地引擎还是云端、
用哪份协议头。它依赖 config.json（CONFIG_FILE 仍归枢纽），所以按惰性导入取用。
"""
import logging

import json
import local_llm

logger = logging.getLogger("latiao-sidecar")   # 与 agent_loop 同名：日志格式不变

async def _resolve_api_target(cloud_config: dict | None) -> tuple[str, str, dict, bool]:
    """Resolve API URL, protocol, headers, and whether it's a local LLM (no cloud config).
    Cloud models are detected by having an endpoint (key is optional for local proxies).

    async：get_api_url 内含同步健康探测（最长 20s + 空闲复验 3s sleep），
    必须放线程池执行，否则阻塞事件循环（P2-13）。"""
    if cloud_config and cloud_config.get("endpoint"):
        protocol = cloud_config.get("protocol", "openai")
        api_url = cloud_config["endpoint"].rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        key = cloud_config.get("key", "")
        if key and protocol != "local":
            headers["Authorization"] = f"Bearer {key}"
        # If the endpoint points to a local server, treat as cloud (native function calling)
        return protocol, api_url, headers, False
    else:
        from starlette.concurrency import run_in_threadpool
        protocol = "openai"
        local_api = await run_in_threadpool(local_llm.get_api_url)
        if local_api:
            api_url = local_api + "/chat/completions"
        else:
            api_url = ""  # No local LLM running — will be caught as connection error
        headers = {"Content-Type": "application/json"}
        return protocol, api_url, headers, True

# 闲聊识别（17:11 事故根治）：任务词表里的"做"字会把"你能做什么"判成任务型
# ——model 因此进入工具结果追问链。闲聊标记优先于任务词：命中即按非任务处理。
def _get_best_cloud_config() -> dict | None:
    """Get the best available cloud model config for code tasks."""
    # CONFIG_FILE 归 agent_loop（枢纽）所有 → 函数内惰性导入，避免模块级回环
    from agent_loop import CONFIG_FILE
    try:
        # First try: config.json cloud_models
        config_file = CONFIG_FILE
        if config_file.exists():
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
            models = cfg.get("cloud_models", [])
            # Prefer models with "mini" or "gpt" in name for code tasks
            for m in models:
                if m.get("endpoint"):
                    return {
                        "endpoint": m["endpoint"],
                        "key": m.get("key", ""),
                        "model": m.get("name", ""),
                        "protocol": m.get("protocol", "openai"),
                    }
            # Fallback: first model with endpoint
            for m in models:
                if m.get("endpoint"):
                    return {
                        "endpoint": m["endpoint"],
                        "key": m.get("key", ""),
                        "model": m.get("name", ""),
                        "protocol": m.get("protocol", "openai"),
                    }
    except Exception:
        logger.warning("Failed to read best cloud config", exc_info=True)
    return None
