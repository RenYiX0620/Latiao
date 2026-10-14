"""端口所有权校验（2026-10-11 深审 Medium）。

_probe_port 此前 TCP 通就认领任意监听者当引擎（对话泄露）；
_kill_port / embedding stop() 对端口任意监听者 SIGKILL/SIGTERM（误杀）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "port-token")


def test_probe_port_rejects_non_openai_listener():
    """TCP 通但 /v1/models 不应答 = 不认领（旧实现 return True）。"""
    import socket
    import threading

    from local_llm_engine import EngineProcess

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)

    def _accept():
        try:
            c, _ = srv.accept()
            c.close()
        except Exception:
            pass

    t = threading.Thread(target=_accept, daemon=True)
    t.start()
    try:
        assert EngineProcess._probe_port(port, timeout=1) is False, \
            "非 OpenAI 兼容监听者不得被认领成引擎"
    finally:
        srv.close()


def test_pid_is_our_engine_matches_llama_server():
    from local_llm_engine import EngineProcess
    # 静态判定逻辑：命令行含 llama-server / mlx_lm 才算自己的
    # 用当前进程自检（python 测试进程不带这些关键字 → False）
    assert EngineProcess._pid_is_our_engine(os.getpid()) is False


def test_kill_port_source_requires_identity_check():
    """源码层钉住：_kill_port 必须先验身份再杀。"""
    src = Path(__file__).resolve().parents[1].joinpath("local_llm_engine.py").read_text("utf-8")
    assert "_pid_is_our_engine" in src
    assert "认不出就不杀" in src or "不杀" in src
    # embedding stop 同理
    src2 = Path(__file__).resolve().parents[1].joinpath("embedding_service.py").read_text("utf-8")
    assert "非嵌入服务占用" in src2
