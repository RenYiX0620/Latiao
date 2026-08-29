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
    if not os.path.isabs(expanded):
        # 相对路径按 sidecar 工作目录解析（此前直接拒绝并误报"路径穿越"）
        try:
            from agent_loop import _safe_cwd
            base = _safe_cwd()
            if not base:
                return None
            expanded = os.path.join(base, expanded)
        except Exception:
            return None
    # Block path traversal — 两种分隔符都查
    if ".." in path.split("/") or ".." in path.split("\\"):
        return None
    # realpath 解析符号链接，防止经由 symlink 逃出校验
    return os.path.realpath(expanded)


def execute(args: dict) -> str:
    p = _safe_path(args["path"])
    if p is None:
        return "⛔ Blocked: 路径无效（空路径或包含 .. 穿越片段）"
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
