"""Search for files matching a glob pattern in a directory (bounded walk)."""
import fnmatch
import os

NAME = "search_files"
PERMISSION = "safe"

DEFINITION = {
    "type": "function",
    "function": {
        "name": "search_files",
        "description": "Search for files matching a glob pattern in a directory. Use this to locate files by name before read_file; do NOT use run_cmd (find/ls | grep) for file lookup.",
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

# 有界遍历（2026-10-24 审计 P2）：与 tool_executor 的 fallback 同一套参数——
# 此前插件用 glob(recursive=True) 全量物化匹配（09-19 卡死 21 分钟事故的正主），
# fallback 的有界修复因"插件先注册"被架空。两份实现必须保持同值，改一处跟一处。
_SEARCH_EXCLUDE_DIRS = frozenset({
    ".lmstudio", "Library", "node_modules", ".git", ".cache", "__pycache__",
    "site-packages", ".venv", "venv", ".Trash", ".npm", ".cargo", ".rustup",
    "Applications", ".zcode", ".cursor", ".vscode", ".docker", ".gradle", ".m2",
    ".ollama", "Photos Library.photoslibrary", "Movies", "Music",
})
_SEARCH_MAX_RESULTS = int(os.environ.get("LATIAO_SEARCH_MAX_RESULTS", "200") or 200)
_SEARCH_MAX_DEPTH = int(os.environ.get("LATIAO_SEARCH_MAX_DEPTH", "6") or 6)


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
    directory = _safe_path(args.get("directory") or args.get("path") or "")
    if directory is None:
        return "⛔ Blocked: path traversal not allowed"
    pattern = str(args.get("pattern") or args.get("query") or "")
    # pattern 不允许是绝对路径或包含 '..'（否则可逃出 directory）
    if os.path.isabs(pattern) or ".." in pattern.split("/") or ".." in pattern.split("\\"):
        return f"⛔ Blocked: pattern 不允许是绝对路径或包含 '..' - {pattern}"
    try:
        if not os.path.isdir(directory):
            return f"No such directory: {directory}"
        pat = pattern.strip()
        if pat.startswith("**/"):
            pat = pat[3:]
        base_depth = directory.rstrip("/").count("/")
        matches, skipped, truncated = [], [], False
        for root, dirs, files in os.walk(directory, topdown=True):
            kept = []
            for d in dirs:
                if d in _SEARCH_EXCLUDE_DIRS or d.endswith(".photoslibrary"):
                    skipped.append(d)
                else:
                    kept.append(d)
            dirs[:] = kept
            if root.count("/") - base_depth >= _SEARCH_MAX_DEPTH:
                dirs[:] = []
            for name in list(files) + list(dirs):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, directory)
                if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
                    matches.append(full)
                    if len(matches) >= _SEARCH_MAX_RESULTS:
                        truncated = True
                        break
            if truncated:
                break
        if not matches:
            note = (f"\n（已跳过 {len(set(skipped))} 类大目录，如 {', '.join(sorted(set(skipped))[:4])}）"
                    if skipped else "")
            return f"No files matching '{pattern}' found in {directory}{note}"
        lines = [f"  {'📁' if os.path.isdir(m) else '📄'} {m}" for m in sorted(matches)[:50]]
        result = f"Search results for '{pattern}' in {directory}:\n" + "\n".join(lines)
        if len(matches) > 50:
            result += f"\n  ... and {len(matches) - 50} more results"
        if truncated:
            result += (f"\n⚠️ 结果已达上限 {_SEARCH_MAX_RESULTS} 条并截断（或深度超过 {_SEARCH_MAX_DEPTH} 层），"
                       "请缩小目录或把模式写得更具体。")
        if skipped:
            result += f"\n（已跳过 {', '.join(sorted(set(skipped))[:5])} 等大目录）"
        return result
    except Exception as e:
        return f"Error searching files: {e}"
