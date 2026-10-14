"""本机语音服务（Qwen3-TTS shim）的生命周期：辣条启动时拉起、退出时停掉。

背景（2026-10-14 用户拍板）：此前该服务由 launchd（`ai.latiao.tts`，RunAtLoad +
KeepAlive）在**登录时**拉起，与辣条无关地常驻约 6.2GB（VoiceDesign + CustomVoice
两个模型各 ~2.9G），退出辣条也不回收——用户看到"辣条关了还占着 6.1GB"。改成
"辣条启动时启动、退出时就退出"后，由这个模块负责：

- **启动**：lifespan 启动钩子里 `start_in_background()`（不阻塞启动）；朗读探测
  失败时也补一次（服务崩了能自愈，不下沉到"永远系统语音"）。
- **停止**：三层保障，覆盖强杀——
  ① 正常退出：Rust 退出前发 SIGTERM 并给 3 秒（`kill_sidecar_on_exit` 轮询
     30×100ms），lifespan 关闭钩子调 `stop()`；
  ② 宿主被强杀（无 SIGTERM）：sidecar 自己的孤儿看门狗里也调 `stop()`；
  ③ sidecar 也被 SIGKILL（如 `restart_sidecar` 的 detach 重启）：拉起子进程时
     注入 `LATIAO_TTS_PARENT_WATCH=1`，shim 侧的父进程看门狗（shim.py）发现
     PPID 变化即自灭——这直接堵的就是"孤儿常驻"那一类。

管理范围由 config.json 的 `tts.service_dir` 决定（空＝不管理，只按 base_url 代理，
其他装机不受影响；`LATIAO_TTS_SERVICE_DIR` 环境变量可覆盖，测试/E2E 用）。服务在
应用包外、只在本机存在——这也是为什么路径必须来自配置而不是硬编码。

与 embedding_service 的三点差异（照抄它反而会错）：
- 服务由外部目录的 start.sh 拉起（脚本自带"清理 7799 旧占用"逻辑），不是包内二进制；
- **不设空闲自动停**：两个模型 6.2GB、重载 10~40s，用户要的是"点了就有"；
  生命周期直接跟应用走，不需要 15 分钟空闲那套；
- `stop()` **只停本进程拉起的那个**，不做"按端口兜底清理"——历史上那正是
  误杀无关监听者的坑（见 embedding_service.stop 的 2026-10-11 注释）。
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("latiao-sidecar")

START_TIMEOUT = 60               # 首次启动含 import mlx_audio（实测端口可连 ~2s）
_START_RETRY_COOLDOWN = 300      # 启动失败后 5 分钟内不再重试（避免每次朗读都卡一下）
_STOP_GRACE = 1.5                # SIGTERM 后等待窗口：Rust 给关闭钩子的总预算是 3s

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_start_failed_at = 0.0
_warm_thread: threading.Thread | None = None


def _conf(conf: dict | None = None) -> dict:
    """生效配置：显式传入（测试）或 config.json 合并默认值，env 覆盖 service_dir。

    覆盖顺序与代码库既有约定一致（env > config，见 embedding_service 的
    LATIAO_EMBED_DIR / LATIAO_SEMANTIC_SEARCH）：LATIAO_TTS_SERVICE_DIR 供
    测试与 E2E 用，避免它们去动真机 config.json。
    """
    merged: dict
    if conf is not None:
        merged = dict(conf)
    else:
        merged = {}
        try:
            from config import CONFIG_FILE
            from tts_service import read_tts_config
            merged = read_tts_config(CONFIG_FILE)
        except Exception:
            logger.debug("读取 tts 配置失败", exc_info=True)
    env_dir = (os.environ.get("LATIAO_TTS_SERVICE_DIR") or "").strip()
    if env_dir:
        merged["service_dir"] = env_dir
    return merged


def service_dir(conf: dict | None = None) -> Path | None:
    """本机语音服务目录（含 start.sh）；空＝不管理。"""
    raw = str(_conf(conf).get("service_dir") or "").strip()
    if not raw:
        return None
    return Path(os.path.expanduser(raw))


def available(conf: dict | None = None) -> bool:
    """配了服务目录且有 start.sh 才管理；其他装机（service_dir 为空）永远 False。"""
    c = _conf(conf)
    if c.get("enabled") is False:
        return False
    d = service_dir(c)
    return bool(d and (d / "start.sh").is_file())


def _base_url(conf: dict | None = None) -> str:
    return str(_conf(conf).get("base_url") or "").strip()


def is_up(conf: dict | None = None, timeout: float = 1.5) -> bool:
    """端口在监听即视为在跑。

    刻意用裸 TCP 连接而不是 GET /health：这里判的是"进程活着没有"（生命周期），
    "服务能不能用"由 tts_service.probe_service 判——两层各管各的，也便于测试里
    用最朴素的假服务替身。
    """
    base = _base_url(conf)
    if not base:
        return False
    try:
        u = urlparse(base)
        host = u.hostname or "127.0.0.1"
        port = u.port
    except Exception:
        return False
    if not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def start(conf: dict | None = None) -> bool:
    """拉起本机语音服务。幂等：本进程已拉起且存活 → 直接成功。

    不做 is_up() 短路：应用启动时若端口上已有**别处遗留**的实例（强杀后的孤儿、
    手工启动），start.sh 自己会先清掉再起（脚本内注释），这样"应用启动 ⇒ 服务
    由本进程拥有"是确定性的，避免"以为在用旧实例、其实它下一秒就退了"的竞态。
    """
    global _proc, _start_failed_at
    c = _conf(conf)
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return True
        if not available(c):
            logger.info("本机语音服务未配置（tts.service_dir 为空或缺 start.sh），不管理")
            return False
        if time.time() - _start_failed_at < _START_RETRY_COOLDOWN:
            return False
        d = service_dir(c)
        assert d is not None  # available() 已保证
        try:
            # 子进程 env 白名单：语音服务不需要 sidecar 的凭据；DYLD_/MTL_/HF_ 等
            # 运行时前缀照 embedding_service 的先例透传（mlx 后端要用）。
            # LATIAO_TTS_PARENT_WATCH=1 是 shim 侧父进程看门狗的总开关（见 shim.py）。
            from cmd_safety import child_env
            from local_llm import _ENGINE_ENV_PREFIXES
            env = child_env(extra={"LATIAO_TTS_PARENT_WATCH": "1"},
                            allow_prefixes=_ENGINE_ENV_PREFIXES)
        except Exception:
            logger.warning("本机语音服务启动失败（env 白名单不可用）", exc_info=True)
            _start_failed_at = time.time()
            return False
        try:
            with open(d / "service.log", "ab") as out, \
                    open(d / "service.err.log", "ab") as err:
                _proc = subprocess.Popen(
                    ["/bin/sh", str(d / "start.sh")],
                    cwd=str(d), env=env,
                    stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                    # 独立进程组：停服务时只针对它自己，绝不牵连 sidecar 进程组
                    start_new_session=True,
                )
        except Exception:
            logger.warning("本机语音服务启动失败", exc_info=True)
            _start_failed_at = time.time()
            return False
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if is_up(c):
                logger.info("本机语音服务已就绪: %s（%s）", d, _base_url(c) or "?")
                return True
            if _proc.poll() is not None:
                logger.warning("本机语音服务进程提前退出（exit=%s），见 %s/service.err.log",
                               _proc.returncode, d)
                break
            time.sleep(0.5)
        else:
            logger.warning("本机语音服务启动超时（%ds）", START_TIMEOUT)
        _terminate_locked()
        _start_failed_at = time.time()
        return False


def start_in_background(conf: dict | None = None) -> None:
    """后台起服务（不阻塞调用方）——应用启动与朗读探测失败都走它。"""
    global _warm_thread
    if _warm_thread is not None and _warm_thread.is_alive():
        return
    _warm_thread = threading.Thread(target=start, args=(conf,),
                                    name="tts-service-warmup", daemon=True)
    _warm_thread.start()


def stop(conf: dict | None = None) -> None:
    """停掉本机语音服务（只停本进程拉起的那个；没拉起就什么都不做）。"""
    with _lock:
        _terminate_locked()


def _terminate_locked() -> None:
    global _proc
    if _proc is None:
        return
    p = _proc
    _proc = None
    try:
        p.terminate()                      # SIGTERM：uvicorn 优雅关闭
        try:
            p.wait(timeout=_STOP_GRACE)
        except subprocess.TimeoutExpired:
            p.kill()                       # 兜底强杀；模型内存随进程退出释放
            p.wait(timeout=2)
    except Exception:
        logger.debug("停止本机语音服务时出错", exc_info=True)
    logger.info("本机语音服务已停止")
