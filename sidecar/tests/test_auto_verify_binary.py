"""自动校验不得打死整轮（2026-09-24 用户实测：写 Word 文档 → .docx 回读崩溃）。

事故：`verify._auto_verify` 的 write_file 回读只处理 FileNotFoundError，而 .docx 是
ZIP 二进制 → `UnicodeDecodeError` 冒到 Agent 循环 → 用户看到"Agent 循环内部错误"，
"以 Word 格式分析今天大盘"这类正常请求直接失败。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "verify-token")


@pytest.mark.asyncio
async def test_binary_deliverable_skips_text_readback(tmp_path):
    from agent.verify import _auto_verify
    docx = tmp_path / "A股分析.docx"
    docx.write_bytes(b"PK\x03\x04\xb3\x00rest-of-zip")   # 伪 docx（含非 UTF-8 字节）

    report = await _auto_verify("write_file", {"path": str(docx), "content": "x"}, "")
    assert "二进制文件" in report
    assert "❌" not in report          # 不该报失败
    assert "文件存在" not in report     # 也不该说文件不存在


@pytest.mark.asyncio
async def test_non_utf8_content_does_not_raise(tmp_path):
    from agent.verify import _auto_verify
    f = tmp_path / "gbk.txt"
    f.write_bytes("中文".encode("gbk"))     # GBK 文本：扩展名认不出，但读不了 UTF-8

    report = await _auto_verify("write_file", {"path": str(f), "content": "中文"}, "")
    assert "不是 UTF-8" in report
    assert "❌" not in report


@pytest.mark.asyncio
async def test_text_file_still_compared(tmp_path):
    from agent.verify import _auto_verify
    f = tmp_path / "note.md"
    f.write_text("hello 辣条\n第二行", encoding="utf-8")

    report = await _auto_verify(
        "write_file", {"path": str(f), "content": "hello 辣条\n第二行"}, "")
    assert "内容一致" in report          # 文本文件仍走真比对
    assert "二进制文件" not in report


@pytest.mark.asyncio
async def test_missing_file_still_reports_failure(tmp_path):
    from agent.verify import _auto_verify
    missing = tmp_path / "nope.txt"
    report = await _auto_verify("write_file", {"path": str(missing), "content": "x"}, "")
    assert "❌" in report and "文件存在" in report
