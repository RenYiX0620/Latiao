"""Open a folder in Finder (macOS only)."""
import os
import platform
import subprocess

NAME = "open_folder"
PERMISSION = "confirm"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "open_folder",
        "description": "Open a folder in Finder (macOS only).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the folder to open."}
            },
            "required": ["path"]
        }
    }
}

IS_MACOS = platform.system() == "Darwin"


def _safe_path(path: str) -> str | None:
    """返回规范化后的绝对路径；不合法返回 None。"""
    if not path:
        return None
    expanded = os.path.expanduser(path)
    # 必须本身就是绝对路径（realpath 会把相对路径变成绝对路径，所以先查）
    if not os.path.isabs(expanded):
        return None
    # Block path traversal — 两种分隔符都查
    if ".." in path.split("/") or ".." in path.split("\\"):
        return None
    # realpath 解析符号链接，防止经由 symlink 逃出校验
    return os.path.realpath(expanded)


def execute(args: dict) -> str:
    p = _safe_path(args["path"])
    if p is None:
        return "⛔ Blocked: path traversal not allowed"
    if IS_MACOS:
        subprocess.Popen(["open", p])
        return f"✅ 已在 Finder 中打开：{p}"
    # Fallback: list directory on non-macOS
    try:
        entries = os.listdir(p)
        lines = [f"  {'📁' if os.path.isdir(os.path.join(p, e)) else '📄'} {e}"
                 for e in sorted(entries)]
        return "目录内容:\n" + "\n".join(lines)
    except Exception as e:
        return f"错误：{e}"
