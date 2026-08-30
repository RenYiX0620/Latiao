#!/usr/bin/env python3
"""鼠标/屏幕控制共享逻辑（macOS CoreGraphics via ctypes，无 pyobjc 依赖）。

权限说明（TCC）：
- 辅助功能（鼠标移动/点击/键盘事件注入）：AXIsProcessTrusted 检测
- 屏幕录制（截屏）：screencapture 失败时提示授权
未授权返回中文指引字符串，不抛异常。
"""
import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# ── CoreGraphics / ApplicationServices ctypes 绑定（惰性，仅 mac 加载） ──
_has_cg = False
_cg = None
try:
    if sys.platform == "darwin":
        _cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        _has_cg = True
except Exception:
    _cg = None
    _has_cg = False

# CGEventType
_kCGEventMouseMoved = 5
_kCGEventLeftMouseDown = 1
_kCGEventLeftMouseUp = 2
_kCGEventRightMouseDown = 3
_kCGEventRightMouseUp = 4
_kCGEventOtherMouseDown = 25
_kCGEventOtherMouseUp = 26
_kCGEventKeyDown = 10
_kCGEventKeyUp = 11
# CGMouseButton
_kCGMouseButtonLeft = 0
_kCGMouseButtonRight = 1
# 修饰键（CGEventFlags）
_kCGFlagShift = 1 << 17
_kCGFlagCommand = 1 << 20
_kCGFlagAlternate = 1 << 19
_kCGFlagControl = 1 << 18

_MODIFIER_MAP = {
    "shift": _kCGFlagShift,
    "cmd": _kCGFlagCommand,
    "command": _kCGFlagCommand,
    "alt": _kCGFlagAlternate,
    "option": _kCGFlagAlternate,
    "opt": _kCGFlagAlternate,
    "ctrl": _kCGFlagControl,
    "control": _kCGFlagControl,
}


def is_macos() -> bool:
    return sys.platform == "darwin"


def tcc_ax_trusted() -> bool:
    """辅助功能权限检测（仅 macOS）。"""
    if not is_macos():
        return False
    try:
        app_services = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        app_services.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(app_services.AXIsProcessTrusted())
    except Exception:
        return False


def tcc_screen_capture_ok() -> bool:
    """屏幕录制权限：通过一次 1x1 截屏试探。"""
    if not is_macos():
        return False
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        r = subprocess.run(
            ["screencapture", "-x", "-R0,0,1,1", tmp.name],
            capture_output=True, text=True, timeout=10,
        )
        ok = r.returncode == 0 and Path(tmp.name).exists() and Path(tmp.name).stat().st_size > 0
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
        return ok
    except Exception:
        return False


def ax_guide() -> str:
    return (
        "⛔ 未授权「辅助功能」。请在系统设置 → 隐私与安全 → 辅助功能 中，"
        "给 Latiao（或 Sidecar 进程）打勾，然后重启应用。"
    )


def screen_guide() -> str:
    return (
        "⛔ 未授权「屏幕录制」。请在系统设置 → 隐私与安全 → 屏幕录制 中，"
        "给 Latiao（或 Sidecar 进程）打勾，然后重启应用。"
    )


def display_bounds() -> tuple[int, int]:
    """主屏宽高（mac：CGMainDisplayBounds）。"""
    if not is_macos():
        return 0, 0
    try:
        _cg.CGMainDisplayBounds.restype = ctypes.c_void_p
        ptr = _cg.CGMainDisplayBounds()
        # CGRect 结构读 width/height（offset 16,24 为宽高，双精度）
        data = ctypes.string_at(ptr or 0, 32)
        import struct
        x, y, w, h = struct.unpack("dddd", data)
        _cg.CGDisplayBoundsRelease(ptr) if hasattr(_cg, "CGDisplayBoundsRelease") else None
        return int(w), int(h)
    except Exception:
        return 0, 0


def screen_capture(save_path: str = "", x: int = 0, y: int = 0, w: int = 0, h: int = 0) -> str:
    """截屏保存 PNG。区域默认全屏；窗口坐标校验在调用方做。"""
    if not is_macos():
        return "⛔ 截屏暂仅支持 macOS"
    if not tcc_screen_capture_ok():
        return screen_guide()
    if not save_path:
        save_path = str(Path.home() / ".local-ai-os" / "screens" / f"cap_{int(__import__('time').time())}.png")
    p = Path(save_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        if w > 0 and h > 0:
            region = f"-R{x},{y},{w},{h}"
            r = subprocess.run(["screencapture", "-x", region, str(p)], capture_output=True, text=True, timeout=15)
        else:
            r = subprocess.run(["screencapture", "-x", str(p)], capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not p.exists() or p.stat().st_size == 0:
            return f"截屏失败: {r.stderr.strip()[:200] or '无输出'}"
        return f"✅ 已保存截屏: {p.resolve()}（{p.stat().st_size} 字节）。请用 read_file 查看内容或继续操作。"
    except Exception as e:
        return f"截屏失败: {e}"


def _requires_ax() -> str | None:
    if not is_macos():
        return "⛔ 鼠标/键盘控制暂仅支持 macOS"
    if not tcc_ax_trusted():
        return ax_guide()
    return None


def validate_coord(x: int, y: int, allow_negative: bool = False) -> str | None:
    if x < 0 or y < 0:
        return f"⛔ 坐标不能为负: ({x},{y})"
    w, h = display_bounds()
    if w > 0 and (x > w or y > h):
        return f"⛔ 坐标越界: ({x},{y})，屏幕分辨率 {w}x{h}"
    return None


def mouse_move(x: int, y: int) -> str:
    e = _requires_ax()
    if e:
        return e
    e = validate_coord(x, y)
    if e:
        return e
    try:
        _cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        _cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
        _cg.CGEventPost.restype = None
        _cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        event = ctypes.c_void_p(_cg.CGEventCreateMouseEvent(None, _kCGEventMouseMoved, x, y, _kCGMouseButtonLeft))
        _cg.CGEventPost(0, event)
        _cg.CFRelease(event) if hasattr(_cg, "CFRelease") else None
        return f"✅ 鼠标已移动到 ({x},{y})"
    except Exception as ex:
        return f"鼠标移动失败: {ex}"


def mouse_click(x: int | None = None, y: int | None = None, button: str = "left", double: bool = False) -> str:
    e = _requires_ax()
    if e:
        return e
    if x is None or y is None:
        # 未提供坐标则点击当前位置（只读当前）
        return "❌ 请提供点击坐标 x 和 y（或先用屏幕截图确定位置）"
    e = validate_coord(x, y)
    if e:
        return e
    try:
        down_type = _kCGEventLeftMouseDown
        up_type = _kCGEventLeftMouseUp
        cg_button = _kCGMouseButtonLeft
        if button == "right":
            down_type, up_type = _kCGEventRightMouseDown, _kCGEventRightMouseUp
            cg_button = _kCGMouseButtonRight
        _cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        _cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
        _cg.CGEventPost.restype = None
        _cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        for _ in range(2 if double else 1):
            down = ctypes.c_void_p(_cg.CGEventCreateMouseEvent(None, down_type, x, y, cg_button))
            _cg.CGEventPost(0, down)
            up = ctypes.c_void_p(_cg.CGEventCreateMouseEvent(None, up_type, x, y, cg_button))
            _cg.CGEventPost(0, up)
        label = "双击" if double else "点击"
        return f"✅ 已{label} ({x},{y}) {button}"
    except Exception as ex:
        return f"鼠标点击失败: {ex}"


def keyboard_type(text: str) -> str:
    e = _requires_ax()
    if e:
        return e
    if not text:
        return "❌ text 不能为空"
    if len(text) > 500:
        return "❌ text 过长（≤500 字符）"
    try:
        # 逐个字符生成 keyDown/keyUp（不模拟组合键，字母数字直接映射）
        cf_str = ctypes.c_char_p(text.encode("utf-8"))
        unicode_str = ctypes.c_void_p(_cg.CFStringCreateWithCString(None, cf_str, 0x08000100)) if hasattr(_cg, "CFStringCreateWithCString") else None
        # 简化：直接用 CGEventKeyboardSetUnicodeString 走 unicode 输入
        _cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        _cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        _cg.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        _cg.CGEventPost.restype = None
        _cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        for ch in text:
            ev = ctypes.c_void_p(_cg.CGEventCreateKeyboardEvent(None, 0, True))
            data = ctypes.create_string_buffer(ch.encode("utf-8"))
            _cg.CGEventKeyboardSetUnicodeString(ev, len(ch.encode("utf-8")), data)
            _cg.CGEventPost(0, ev)
            ev_up = ctypes.c_void_p(_cg.CGEventCreateKeyboardEvent(None, 0, False))
            _cg.CGEventPost(0, ev_up)
        return f"✅ 已键入 {len(text)} 个字符"
    except Exception as ex:
        return f"键入失败: {ex}"


def keyboard_press(combo: str) -> str:
    """快捷键组合，如 cmd+tab / shift+cmd+A；按 keyCode 映射常见键。"""
    e = _requires_ax()
    if e:
        return e
    keys = [k.strip().lower() for k in (combo or "").split("+") if k.strip()]
    if not keys:
        return "❌ combo 不能为空（如 cmd+tab）"
    flags = 0
    key_codes = {
        "tab": 48, "a": 0, "c": 8, "v": 9, "z": 6, "x": 7, "w": 13, "q": 12,
        "space": 49, "return": 36, "enter": 36, "escape": 53, "esc": 53,
        "left": 123, "right": 124, "up": 126, "down": 125,
        "h": 4, "j": 38, "k": 40, "l": 37, "f": 3, "p": 35, "s": 1, "d": 2,
    }
    non_mod_keys = [k for k in keys if k not in _MODIFIER_MAP]
    if len(non_mod_keys) != 1 or non_mod_keys[0] not in key_codes:
        return f"❌ 无法识别组合 {combo}（支持 cmd+tab / cmd+w / cmd+shift+3 等，最多一个主键）"
    for k in keys:
        if k in _MODIFIER_MAP:
            flags |= _MODIFIER_MAP[k]
    key_code = key_codes[non_mod_keys[0]]
    try:
        _cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        _cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        _cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        _cg.CGEventPost.restype = None
        _cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        down = ctypes.c_void_p(_cg.CGEventCreateKeyboardEvent(None, key_code, True))
        _cg.CGEventSetFlags(down, flags)
        _cg.CGEventPost(0, down)
        up = ctypes.c_void_p(_cg.CGEventCreateKeyboardEvent(None, key_code, False))
        _cg.CGEventSetFlags(up, flags)
        _cg.CGEventPost(0, up)
        return f"✅ 已发送快捷键 {combo}"
    except Exception as ex:
        return f"快捷键失败: {ex}"
