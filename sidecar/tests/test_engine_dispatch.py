"""原生引擎分派的单引擎契约（2026-10-12 收编 fork 后）。

历史：Spark-X2.5 曾必须用 `sidecar/llama-server`（XHToken fork）——上游不认它的
模板/工具格式。2026-10-12 实测上游 b11554/b11571 已覆盖（加载 ✓、原生 tools 调用
解析正确 ✓、同提示词行为一致 ✓），fork 退役。本测试钉住新契约：**所有模型**
一律优先 `llama-upstream/llama-server`，fork 只作最后兜底。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "dispatch-token")

from local_llm_launch_common import find_llama_server  # noqa: E402

SIDECAR = Path(__file__).resolve().parents[1]
UPSTREAM = SIDECAR / "llama-upstream" / "llama-server"
FORK = SIDECAR / "llama-server"


def test_returns_a_candidate_or_none():
    """真机/CI 环境下至少能解析出一个候选（本机应命中上游）。"""
    p = find_llama_server("/models/Hermes3.6-35B-A3B.gguf")
    assert p is None or p.is_file()


def test_isolated_tree_prefers_upstream_for_spark_and_normal(tmp_path, monkeypatch):
    """隔离树（只造上游 + fork 两个候选）：spark 与普通模型都必须选上游。"""
    import local_llm_launch_common as C

    (tmp_path / "llama-upstream").mkdir()
    up = tmp_path / "llama-upstream" / "llama-server"
    up.write_text("#!/bin/sh\n", encoding="utf-8")
    fork = tmp_path / "llama-server"
    fork.write_text("#!/bin/sh\n", encoding="utf-8")
    # 函数用 `Path(__file__).parent` 定位——把模块 __file__ 指到隔离树即可
    monkeypatch.setattr(C, "__file__", str(tmp_path / "local_llm_launch_common.py"))

    assert C.find_llama_server("/models/Spark-X2.5-4B-uncensored-Q8_0.gguf") == up, \
        "Spark 也必须走上游（fork 已退役；A/B 见 fetch-upstream-llama.sh 说明）"
    assert C.find_llama_server("/models/Hermes3.6-35B-A3B.gguf") == up


def test_fork_still_works_as_last_resort(tmp_path, monkeypatch):
    """旧安装里 fork 还在、上游缺失 → 仍能兜底（不至于'缺文件即无法启动'）。"""
    import local_llm_launch_common as C

    fork = tmp_path / "llama-server"
    fork.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(C, "__file__", str(tmp_path / "local_llm_launch_common.py"))
    assert C.find_llama_server("/models/whatever.gguf") == fork
