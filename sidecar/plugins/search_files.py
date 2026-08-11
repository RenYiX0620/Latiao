"""Search for files matching a glob pattern in a directory."""
import glob as glob_mod
import os

NAME = "search_files"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "search_files",
        "description": "Search for files matching a glob pattern in a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Absolute path to the directory to search in."},
                "pattern": {"type": "string", "description": "Glob pattern to match (e.g., '*.py', '**/*.md')."}
            },
            "required": ["directory", "pattern"]
        }
    }
}


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
    directory = _safe_path(args["directory"])
    if directory is None:
        return "⛔ Blocked: path traversal not allowed"
    pattern = args["pattern"]
    # pattern 不允许是绝对路径或包含 '..'（否则可逃出 directory）
    if os.path.isabs(pattern) or ".." in pattern.split("/") or ".." in pattern.split("\\"):
        return f"⛔ Blocked: pattern 不允许是绝对路径或包含 '..' - {pattern}"
    try:
        search_path = os.path.join(directory, pattern)
        matches = glob_mod.glob(search_path, recursive=True)
        if not matches:
            return f"No files matching '{pattern}' found in {directory}"
        lines = []
        for m in sorted(matches)[:50]:
            icon = "📁" if os.path.isdir(m) else "📄"
            lines.append(f"  {icon} {m}")
        result = f"Search results for '{pattern}' in {directory}:\n" + "\n".join(lines)
        if len(matches) > 50:
            result += f"\n  ... and {len(matches) - 50} more results"
        return result
    except Exception as e:
        return f"Error searching files: {e}"
