"""插件静态审查的绕过修复回归（2026-10-11 第三轮审计）。

审计原文：审查还是 Call-name 黑名单——`getattr(os,"system")`、别名、
`builtins.eval`、裸 `subprocess.run` 都能过。实测全部成立（这四条修前均放行）。
修法：反射入口本身入名单 + getattr 字符串参数判定 + 导入/赋值别名解析 +
拼接/f-string 动态构造检测。这里用正控（必须拦）与反控（正常插件不得误伤）钉住。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LATIAO_AUTH_TOKEN", "sec-plug-token")


def _denied(code: str) -> bool:
    from plugin_creator import validate_plugin_code
    v = validate_plugin_code(code)
    return any("禁止" in e for e in (v.get("errors") or []))


@pytest.mark.parametrize("code", [
    'getattr(os,"system")("id")',
    'import builtins; builtins.eval("1+1")',
    'import builtins; builtins.__import__("os")',
    'f = getattr(__builtins__,"eval"); f("1")',
    'import os as o; o.system("id")',
    'import subprocess; subprocess.run(["id"])',
    'x = os.system; x("id")',
    'g = getattr(os, "sys"+"tem")',
    'import os; os.system("id")',            # 原有能力保持
    '__import__("os").system("id")',         # 原有能力保持
])
def test_reflection_and_alias_bypasses_blocked(code):
    assert _denied(code), f"应被拦: {code}"


def test_normal_plugin_not_broken():
    good = '''
NAME = "demo"
DEFINITION = {"description": "demo"}
PERMISSION = "readonly"

def execute(args: dict) -> str:
    import json
    data = json.loads(args.get("json", "{}"))
    return "hi " + data.get("name", "")
'''
    assert not _denied(good), "正常插件不得误伤"
