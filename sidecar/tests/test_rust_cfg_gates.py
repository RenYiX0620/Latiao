"""Rust 平台门控护栏（2026-10-11 v0.3.78 Windows 构建事故）。

**事故档案**：`open_path` 里用 `#[cfg(unix)] fn is_executable(...)` 定义了仅在
unix 存在的函数，调用点写成 `if cfg!(unix) && is_executable(p)`——`cfg!` 是
**运行时常量**，被它"保护"的代码在非 unix 目标上**照样参与类型检查**，于是
Windows CI 报 `E0425 cannot find function is_executable` 直接构建失败。
macOS 本机 `cargo check` 全绿（门控为真），CI 才发现——本机没有 Windows SDK，
完整交叉编译也走不通（ring 的 C 代码需要 Windows 头文件）。

**护栏**：源码级断言——main.rs 里不得出现 `cfg!(...)` 直接保护平台专属函数的
写法；`is_executable` 的每个调用点前必须有 `#[cfg(unix)]` 属性。这类错误以后
在本地/CI 的 pytest 阶段就能拦下，不必等 Windows 构建。
"""
from __future__ import annotations

import re
from pathlib import Path

MAIN_RS = Path(__file__).resolve().parents[2] / "src-tauri" / "src" / "main.rs"


def _src() -> str:
    return MAIN_RS.read_text(encoding="utf-8")


def _src_no_comments() -> str:
    """剥掉行注释后的源码——判据只看真代码（注释里会引用事故的旧写法）。"""
    out = []
    for line in _src().splitlines():
        idx = line.find("//")
        out.append(line[:idx] if idx != -1 else line)
    return "\n".join(out)


def test_no_runtime_cfg_guard_on_platform_functions():
    """`cfg!(unix) && f()` 形态：若 f 是 #[cfg] 门控函数，非目标平台会 E0425。

    规则：main.rs 的**代码**里出现 `cfg!(unix)` 或 `cfg!(windows)` 且同一表达式
    里跟了函数调用 → 必须人工确认该函数不是平台门控的；本项目现在的正确答案是
    **一处都不该有**（平台分支用 #[cfg] 属性独占整段，而不是运行时短路）。
    """
    src = _src_no_comments()
    bad = []
    for m in re.finditer(r"cfg!\((?:unix|windows|target_os)[^)]*\)\s*&&\s*([A-Za-z_][\w:]*)",
                         src):
        bad.append(m.group(0))
    assert not bad, (
        "发现 cfg!(...) 运行时短路保护平台函数调用——请改用 #[cfg(...)] 属性门控："
        + "; ".join(bad))


def test_is_executable_call_sites_attribute_gated():
    """`is_executable` 是 #[cfg(unix)] 函数：每个调用点必须由 #[cfg(unix)] 属性保护。"""
    src = _src_no_comments()
    assert "#[cfg(unix)]\nfn is_executable" in src, "定义处应带 #[cfg(unix)]"
    def_pos = src.index("fn is_executable")
    for m in re.finditer(r"is_executable\(", src):
        start = m.start()
        if abs(start - def_pos) < 40:
            continue  # 定义本身
        window = src[max(0, start - 200):start]
        assert "#[cfg(unix)]" in window, (
            f"is_executable 调用点缺少 #[cfg(unix)] 属性保护（offset {start}）："
            "Windows 构建会 E0425")
