"""本地嵌入服务（2026-09-23，审查⑥）。

为什么需要：检索此前是字符级词频（`_tokenize_zh` + TF-IDF 余弦）——真库实测 9 个
真实查询只有 3 个能命中（"板块资金流向怎么看"命不中"主力净流入资金"那类知识）。
加入语义检索后同一批查询命中 6/9（见 scripts/eval_semantic_recall.py 的量表）。

模型选择（两个候选都实测过，许可都过关）：
- bge-small-zh-v1.5（MIT，46MB）：命中 4/9，但**分离度失败**——无关查询
  "帮我把 Python 代码重构一下"拿到 0.660，比真命中的最低分 0.468 还高，
  配固定阈值必然重演"聊什么它都去找股市"（历史事故），故弃用。
- **Qwen3-Embedding-0.6B（Apache-2.0，610MB Q8）**：命中 6/9，且真命中最低 0.453、
  无关最高 0.481 —— 几乎分开，可按 0.50 标定门槛。选它。
（embeddinggemma 是 Gemma 许可、jina-v3 是 CC-BY-NC，都未采用。）

运行方式：复用应用自带的 llama-server（llama.cpp 支持 --embeddings），独立实例、
独立端口，**按需启动 + 空闲自动停**——不占用聊天引擎，不常驻内存（实测空闲时可停）。
"""
import json
import logging
import os
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("latiao-sidecar")

MODEL_FILENAME = "Qwen3-Embedding-0.6B-Q8_0.gguf"
MODEL_ID = "Qwen3-Embedding-0.6B-Q8_0"
EMBED_PORT = int(os.environ.get("LATIAO_EMBED_PORT", "8897"))
IDLE_STOP_SECONDS = 900          # 空闲 15 分钟停掉，腾内存
START_TIMEOUT = 60               # 首次启动含加载模型

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_last_use = 0.0
_start_failed_at = 0.0
_START_RETRY_COOLDOWN = 300      # 启动失败后 5 分钟内不再重试（避免每次检索都卡一下）


def model_dir() -> Path:
    d = Path(os.environ.get("LATIAO_EMBED_DIR", str(Path.home() / ".local-ai-os" / "embed-models")))
    return d


def model_path() -> Path | None:
    p = model_dir() / MODEL_FILENAME
    return p if p.is_file() else None


def enabled() -> bool:
    """是否启用语义检索：config.json memory.semantic_search（默认 True）。

    模型文件不存在时即便配置为 True 也不可用（由 available() 判定）。
    """
    raw = os.environ.get("LATIAO_SEMANTIC_SEARCH")
    if raw is None:
        try:
            import json as _json
            from config import CONFIG_FILE
            if CONFIG_FILE.exists():
                cfg = _json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                section = cfg.get("memory") if isinstance(cfg, dict) else None
                if isinstance(section, dict) and section.get("semantic_search") is not None:
                    return bool(section.get("semantic_search"))
        except Exception:
            pass
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def available() -> bool:
    return enabled() and model_path() is not None


def _find_server_binary() -> Path | None:
    """复用应用自带的 llama-server（与聊天引擎同一份二进制，避免再装运行时）。"""
    base = Path(__file__).parent
    for exe in (base / "llama-upstream" / "llama-server", base / "llama-server",
                base / "llama-server.exe"):
        try:
            if exe.exists() and exe.is_file():
                return exe
        except OSError:
            continue
    return None


def is_up(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{EMBED_PORT}/health", timeout=timeout):
            return True
    except Exception:
        return False


def start() -> bool:
    """按需启动嵌入服务；已在跑/刚失败过/缺文件都直接返回。"""
    global _proc, _start_failed_at
    if is_up():
        return True
    with _lock:
        if is_up():
            return True
        if time.time() - _start_failed_at < _START_RETRY_COOLDOWN:
            return False
        exe = _find_server_binary()
        mp = model_path()
        if not exe or not mp:
            logger.info("语义检索不可用（缺 llama-server 或嵌入模型）：%s", mp or "(无模型)")
            _start_failed_at = time.time()
            return False
        cmd = [str(exe), "-m", str(mp), "--embeddings", "-c", "1024", "-b", "1024",
               "-ub", "1024", "--port", str(EMBED_PORT), "--host", "127.0.0.1",
               "-ngl", "99", "--no-webui"]
        try:
            # ④ 子进程 env 白名单：模型服务不需要 sidecar 的凭据；与聊天引擎用同一份
            # 运行时前缀（DYLD_/MTL_/GGML_…），否则 Metal 后端可能起不来。
            from cmd_safety import child_env
            from local_llm import _ENGINE_ENV_PREFIXES
            _proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     env=child_env(allow_prefixes=_ENGINE_ENV_PREFIXES))
        except Exception:
            logger.warning("嵌入服务启动失败", exc_info=True)
            _start_failed_at = time.time()
            return False
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if is_up():
                logger.info("嵌入服务就绪: %s（端口 %d）", MODEL_ID, EMBED_PORT)
                return True
            time.sleep(0.4)
        logger.warning("嵌入服务启动超时（%ds）", START_TIMEOUT)
        stop()
        _start_failed_at = time.time()
        return False


def stop() -> None:
    """停掉嵌入服务（空闲自动停 / 关闭应用时调用）。"""
    global _proc
    with _lock:
        if _proc is not None:
            try:
                _proc.terminate()
                _proc.wait(timeout=10)
            except Exception:
                try:
                    _proc.kill()
                except Exception:
                    pass
            _proc = None
        else:
            # 不是本进程启的（例如上一轮遗留）：按端口清理
            try:
                out = subprocess.run(["lsof", "-nP", f"-iTCP:{EMBED_PORT}", "-sTCP:LISTEN", "-t"],
                                     capture_output=True, text=True, timeout=5).stdout.split()
                for pid in out:
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except (ValueError, ProcessLookupError, PermissionError):
                        continue
            except Exception:
                pass
    logger.info("嵌入服务已停止")


_warm_thread: threading.Thread | None = None


def _start_in_background() -> None:
    """后台起服务（不阻塞调用方）——检索路径永远不等冷启动。"""
    global _warm_thread
    if _warm_thread and _warm_thread.is_alive():
        return
    _warm_thread = threading.Thread(target=start, name="embed-warmup", daemon=True)
    _warm_thread.start()


def embed(texts: list[str], timeout: float = 60,
          allow_cold_start: bool = False) -> list[list[float]] | None:
    """批量编码；服务不可用/出错返回 None（调用方 fail-open 回退词频检索）。

    allow_cold_start=False（检索路径默认）：服务没在跑就**后台**去起、本次直接返回
    None——绝不让一次对话卡在"加载嵌入模型"上（冷启动 5~20s）。下一次检索就能用上。
    allow_cold_start=True：预热线程用，等它起来。
    """
    global _last_use
    if not texts or not available():
        return None
    if not is_up():
        if not allow_cold_start:
            _start_in_background()
            return None
        if not start():
            return None
    _last_use = time.time()
    body = json.dumps({"input": texts}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{EMBED_PORT}/v1/embeddings", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        return [e["embedding"] for e in data["data"]]
    except Exception:
        logger.warning("嵌入调用失败（回退词频检索）", exc_info=True)
        return None


def maybe_stop_idle(now: float | None = None) -> bool:
    """空闲超过 IDLE_STOP_SECONDS 就停掉。返回是否停了。"""
    global _last_use
    if not is_up() or _last_use <= 0:
        return False
    if (now or time.time()) - _last_use < IDLE_STOP_SECONDS:
        return False
    logger.info("嵌入服务空闲 %d 秒，自动停止以释放内存",
                int((now or time.time()) - _last_use))
    stop()
    _last_use = 0.0
    return True
