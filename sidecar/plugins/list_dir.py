"""List the contents of a directory."""
import os

NAME = "list_dir"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "List the contents of a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the directory to list."}
            },
            "required": ["path"]
        }
    }
}


def _safe_path(path: str) -> str | None:
    """返回规范化后的绝对路径；不合法返回 None。"""
    if not path:
        return None
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        # 相对路径按 sidecar 工作目录解析：模型常发 "."（列当前目录），
        # 此前直接拒绝并误报"路径穿越"（18:13 事故——自检任务第一步就被拦）
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
    try:
        entries = os.listdir(p)
        lines = [f"  {'📁' if os.path.isdir(os.path.join(p, e)) else '📄'} {e}"
                 for e in sorted(entries)]
        return "目录内容:\n" + "\n".join(lines)
    except Exception as e:
        return f"错误：{e}"
