"""工具失败判据的唯一形状（2026-09-29 审计后收紧）。

`_looks_like_tool_failure` 是**同错升级**（连续同签名 3 轮 → 硬升级交回用户）与
`verify_failed` 的共用判据，所以它误判一句话，用户就可能被无端交回。

原实现是"头 200 字里含 不存在/未找到/权限不足 即失败"。审计用真文案实证了两类
**成功**结果被判失败：读到含这些词的源码、搜索恰好命中这些词（tavily 的空结果
「🔍 …未找到相关结果。」）。收紧后的判据：**首行的失败句形状** + 我们自己的失败
前缀（Error/错误/⛔/❌）+ 结构化 `{"status":"error"}`。

这个文件同时是"真失败不许漏"的正面清单——改动判据时两边都要过。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.tool_exec import _looks_like_tool_failure as is_failure  # noqa: E402

# ── 成功结果：一律不许判失败（每条都有审计实证或真实文案依据）──
_SUCCESS = {
    "读到含「不存在」的源码":
        "文件：/repo/sidecar/api_routes_cron_local.py（96 行）\n"
        "    # 任务不存在时返回错误\n    return {\"status\": \"error\"}\n" + "x" * 300,
    "搜索命中就是「未找到」这两个词":
        "搜索 '未找到' → 3 处命中：\n  local_llm_download.py:522: 未找到下载记录\n" + "y" * 300,
    "tavily 空结果（成功、无命中）":
        "🔍 Tavily 搜索: 今天大盘\n\n未找到相关结果。",
    "目录列表里有个文件名含「不存在」":
        "目录列表：\n  不存在的东西.md\n  正常.md\n" + "z" * 300,
    "正常成功（✅）": "✅ 已写入：/tmp/a.txt（12 字符）",
    "首行是长句（含失败词但在句中）":
        "分析结论：这次查询未找到足够的样本，但已用三个指标交叉验证。" + "w" * 200,
    "空结果": "",
}

_REAL_FAILURES = {
    "中文模板（错误：）": "错误：文件不存在：/tmp/nope.txt",
    "英文（Error）": "Error: ENOENT no such file",
    "工具层标记（⛔）": "⛔ 未知工具 'foo'。可用工具：read_file, write_file",
    "open_app 失败（❌）": "❌ 无法打开 某应用: 应用不存在或无法打开",
    "结构化 dict（status=error）": '{"status": "error", "message": "仓库不存在"}',
    "首行短失败句（权限不足）": "权限不足：无法写入 /etc/hosts",
    "首行「未找到」短句": "未找到匹配的文件",
}


@pytest.mark.parametrize("name,text", sorted(_SUCCESS.items()))
def test_success_results_are_not_failures(name, text):
    assert is_failure(text) is False, f"成功结果被误判成失败：{name}"


@pytest.mark.parametrize("name,text", sorted(_REAL_FAILURES.items()))
def test_real_failures_are_still_caught(name, text):
    assert is_failure(text) is True, f"真失败被判成成功（会漏升级）：{name}"
